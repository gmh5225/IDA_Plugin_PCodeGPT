# -*- coding=utf-8 -*-
import idaapi
import ida_hexrays as hr
import ida_kernwin as kw
import idc
import ida_nalt

import openai
import functools
import textwrap
import threading
import re
import json
import os

gpt_api_key = os.getenv("OPENAI_API_KEY")
if not gpt_api_key:
    gpt_api_key = ''
    if len(gpt_api_key) == 0:
        print('[-] 请输入 OPENAI_API_KEY')
gpt_model = 'gpt-4'
gpt_base_url = ''
# Set your own proxy if necessary. (e.g. Clash)
proxies = {'http': "http://127.0.0.1:7890", 'https': "http://127.0.0.1:7890"}
openai.proxy = proxies
gpt_proxy = proxies

client = openai.OpenAI(
    api_key = gpt_api_key,
    base_url = gpt_base_url,
    http_client = _httpx.Client(
        proxies = gpt_proxy,
    ) if gpt_proxy else None
)

#发送请求给gpt
def query_model(query, cb):
    try:
        response = client.chat.completions.create(
            model = gpt_model,
            messages = [
                {"role": "user", "content": query}
            ]
        )
        
        kw.execute_sync(functools.partial(cb, response=response.choices[0].message.content),
                                 kw.MFF_WRITE)
    except:
        print(f'[-] {gpt_model} querying failed!')

#请求处理入口，通过创建线程进行查询
def query_model_async(query, cb):
    print(f'[+] {gpt_model} querying ...')
    t = threading.Thread(target=query_model, args=[query, cb])
    t.start()

#接收response并对子函数进行重命名
def subfunction_rename_callback(vu, response, retries=0):

    j = re.search(r"\{[^}]*?\}", response)
        
    try:
        names = json.loads(j.group(0))
    except:
        if retries < 3:
            retries += 1
            print(f'[-] 子函数重命名失败, 第{retries}次重试中...')
            query_model_async("这个文本中的JSON文档无效，你能修好它吗？\n" + response,
                          functools.partial(subfunction_rename_callback,
                                            vu=vu,
                                            retries=retries))
        else:
            print(f'[-] 子函数重命名失败，请检查代码中是否存在以"sub_"开头的子函数后重试')
        return
    
    for n in names:
        if names[n].startswith('sub_'):
            # Extract the part after the underscore and convert to integer
            subfunction_ea = int(names[n].split('_', 1)[1], 16)
            subfunction_pc = str(hr.decompile(subfunction_ea))
            function_rename_query(vu, subfunction_ea, subfunction_pc)

#接收response并对变量进行重命名
def variable_rename_callback(vu, response, retries=0):

    j = re.search(r"\{[^}]*?\}", response)
        
    try:
        names = json.loads(j.group(0))
    except:
        if retries < 3:
            retries += 1
            print(f'[-] 变量重命名失败, 第{retries}次重试中...')
            query_model_async("这个文本中的JSON文档无效，你能修好它吗？\n" + response,
                          functools.partial(variable_rename_callback,
                                            vu=vu,
                                            retries=retries))
        else:
            print(f'[-] 变量重命名失败，请检查代码中是否存在变量后重试')
        return
    
    prefixes = ["unk_", "byte_", "word_", "dword_", "qword_"]
    
    # The rename function needs the start address of the function
    function_addr = vu.cfunc.entry_ea
    for n in names:
        if hr.rename_lvar(function_addr, n, names[n]) == False:
            for prefix in prefixes:
                if n.startswith(prefix):
                    # Extract the part after the underscore and convert to integer
                    ea = int(n.split('_', 1)[1], 16)
                    idaapi.set_name(ea, names[n], 0)

    vu.refresh_view(True)
        
    print(f"[+] {gpt_model} 重命名了 {len(names)} 个变量!")

#在伪代码的指定位置添加注释
def set_pseudocode_cmt(vu, ea, cmt):
    commentSet = False
    eamap = vu.cfunc.get_eamap()
    tl = idaapi.treeloc_t()
    tl.ea = eamap[ea][0].ea
    tl.itp = idaapi.ITP_SEMI
    vu.cfunc.set_user_cmt(tl, cmt)
    vu.cfunc.save_user_cmts()

    for itp in [idaapi.ITP_SEMI, idaapi.ITP_BRACE2]:
        tl.itp = itp
        vu.cfunc.set_user_cmt(tl, cmt)
        if vu.cfunc.get_user_cmt(tl, idaapi.RETRIEVE_ONCE) != None:
            commentSet = True
            break
    vu.cfunc.del_orphan_cmts()
    vu.cfunc.save_user_cmts()
    if not commentSet:
        print ("pseudo comment error at %08x" % ea)
    else:
        vu.refresh_ctext()

def get_item_ea(vu, line):
    tag = idaapi.COLOR_ON + chr(idaapi.COLOR_ADDR)
    pos = line.find(tag)
    cur_col = pos+len(tag)
    
    while pos != -1 and len(line[cur_col:]) >= idaapi.COLOR_ADDR_SIZE:
        addr = line[cur_col:cur_col+idaapi.COLOR_ADDR_SIZE]
        idx = int(addr, 16)
        ca = idaapi.ctree_anchor_t()
        ca.value = idx
        if ca.is_valid_anchor() and ca.is_citem_anchor():
            item = vu.cfunc.treeitems.at(idx)
            if item and item.ea != idaapi.BADADDR:
                return item.ea
        pos = line.find(tag, cur_col+idaapi.COLOR_ADDR_SIZE)
        cur_col = pos+len(tag)
        
    return None

#接收response并为伪代码添加注释
def set_comment_callback(vu, response, retries=0):

    j = re.search(r"\{[^}]*?\}", response)
        
    try:
        comments = json.loads(j.group(0))
    except:
        if retries < 3:
            retries += 1
            print(f'[-] 添加注释失败, 第{retries}次重试中...')
            query_model_async("这个文本中的JSON文档无效，你能修好它吗？\n" + response,
                          functools.partial(set_comment_callback,
                                            vu=vu,
                                            retries=retries))
        else:
            print(f'[-] 添加注释失败，请检查代码是否有误后重试')
        return
    
    entry_ea = vu.cfunc.entry_ea
    func_cmt = idc.get_func_cmt(entry_ea, 0)
    if not func_cmt:
        func_cmt_lines = 0
    else:
        func_cmt_lines = len(func_cmt.split('\n'))
    
    pc = vu.cfunc.get_pseudocode()
        
    for lineno_str in comments:
        lineno = int(lineno_str) + func_cmt_lines
        ea = get_item_ea(vu, pc[lineno].line)
        if ea != None:
            set_pseudocode_cmt(vu, ea, comments[lineno_str])

    vu.refresh_view(True)

    print(f"[+] {gpt_model} comment finished!")

#将文本转换成每line_length一行
def wrap_text(text, line_length):
    pattern = fr"(?:(?!\n).){{1,{line_length}}}"
    return '\n'.join(re.findall(pattern, text))

#接收response并在函数头显示分析的结果
def analyze_code_callback(vu, response):

    entry_ea = vu.cfunc.entry_ea

    idc.set_func_cmt(entry_ea, '', 0)

    wrap_response = wrap_text(response, 64)

    comment = '--------------------PCodeGPT--------------------\n'
    comment += wrap_response
    comment += '\n'
    comment += '--------------------PCodeGPT--------------------'

    idc.set_func_cmt(entry_ea, comment, 0)

    vu.refresh_view(False)

    print(f"[+] {gpt_model} query finished!")

#接收response并对函数进行重命名
def function_rename_callback(vu, ea, pc, response, retries=0):

    j = re.search(r"\{[^}]*?\}", response)
        
    try:
        names = json.loads(j.group(0))
    except:
        if retries < 3:
            retries += 1
            print(f'[-] 函数 sub_{hex(ea)[2:]} 重命名失败, 第{retries}次重试中...')
            query_model_async("这个文本中的JSON文档无效，你能修好它吗？\n" + response,
                          functools.partial(set_comment_callback,
                                            vu=vu,
                                            ea=ea,
                                            pc=pc,
                                            retries=retries))
        else:
            print(f'[-] 函数 sub_{hex(ea)[2:]} 重命名失败，请检查代码是否有误后重试')
        #function_rename_query(vu, ea, pc)
        return
    
    # The rename function needs the start address of the function
    function_addr = ea
    for n in names:
        idaapi.set_name(function_addr, names[n], 0)

    vu.refresh_view(True)
        
    print(f"[+] {gpt_model} 重命名了 sub_{hex(ea)[2:]} 函数!")

#接收response并在函数头显示Python代码
def convert_to_python_callback(vu, response, retries=0):

    # 使用re.findall()来查找匹配的文本
    python_code = re.findall(r'```python\s*(.*?)\s*```', response, re.MULTILINE | re.DOTALL)
    if len(python_code) == 0:
        if retries < 3:
            retries += 1
            print(f'[-] 转换为Python代码失败, 第{retries}次重试中...')
            query_model_async("这个文本中的Python代码无效，你能修好它吗？\n" + response,
                          functools.partial(convert_to_python_callback,
                                            vu=vu,
                                            retries=retries))
        else:
            print(f'[-] 转换为Python代码失败，请检查代码是否有误后重试')
        return

    entry_ea = vu.cfunc.entry_ea
    
    func_name = idaapi.get_name(entry_ea).replace('?', '')

    # 获取当前文件的路径
    file_path = ida_nalt.get_input_file_path()
    base_name, extension = os.path.splitext(file_path)

    fp = f'{base_name}_{func_name}.py'
    with open(fp, 'w') as file:
        file.write(python_code[0].strip())
    #idc.set_func_cmt(entry_ea, comment, 0)

    vu.refresh_view(False)

    print(f"[+] {gpt_model} 已将 Python 代码保存至{fp}!")

def subfunction_rename_query():
    vu = hr.get_widget_vdui(kw.get_current_widget())
    if vu == None: return
    
    pseudocode = str(vu.cfunc)
    
    query_string = '要求：分析下面这段C语言代码，分析它调用了哪些函数名以"sub_"开头的函数。\n' \
                   '注意：只需要函数名以"sub_"开头的函数，并且不要重复。\n' \
                   '重要：回复一个JSON数组，其中键是数字序号，值是函数的名称。\n' \
                   '非常重要：只打印JSON字典，不要有任何多余的字、解释、注意事项等：\n' \
                   + pseudocode
    
    query_model_async(
        query_string,
        functools.partial(subfunction_rename_callback, vu=vu))
        
def variable_rename_query():
    vu = hr.get_widget_vdui(kw.get_current_widget())
    if vu == None: return
    
    pseudocode = str(vu.cfunc)
    
    query_string = '要求：分析下面这段C语言代码，然后为所有变量给出更好的变量名，' \
                   '包括以"off_"、"unk_"、"byte_"、"word_"、"dword_"、"qword_"开头的变量。\n' \
                   '注意1：列出所有可能的变量，包括整数、数组、全局变量等。\n' \
                   '注意2：新的变量名不要使用寄存器名或以"off_"、"unk_"、"byte_"、"word_"、"dword_"、"qword_"开头。' \
                   '重要：回复一个JSON数组，其中键是变量的原始名称，值是建议的名称。\n' \
                   '非常重要：只打印JSON字典，不要有任何多余的字、解释、注意事项等：\n' \
                   + pseudocode
    
    query_model_async(
        query_string,
        functools.partial(variable_rename_callback, vu=vu))
        
def set_comment_query():
    vu = hr.get_widget_vdui(kw.get_current_widget())
    if vu == None: return
    
    pseudocode = str(vu.cfunc)
    
    # Split the string into lines
    lines = pseudocode.split('\n')
    
    lines = [line for line in lines if not line.startswith("//")]
    
    # Add line numbers
    numbered_lines = [f"{i}.{line}" for i, line in enumerate(lines)]

    # Join the lines back into a single string
    numbered_pseudocode = '\n'.join(numbered_lines)
    
    query_string = '要求：使用简体中文为下面这段C语言代码添加注释。\n' \
                   '注意：回复一个JSON数组，其中键是行号，值是该行的注释。\n' \
                   '非常重要：只打印JSON字典，不要有任何多余的字、解释、注意事项等：\n' \
                   + numbered_pseudocode
    
    query_model_async(
            query_string,
            functools.partial(set_comment_callback, vu=vu))

def analyze_code_query():
    vu = hr.get_widget_vdui(kw.get_current_widget())
    if vu == None: return
    
    pseudocode = str(vu.cfunc)
    
    query_string = '分析下面这段C语言代码的详细功能（使用简体中文回答）：\n' + pseudocode
    
    query_model_async(
            query_string,
            functools.partial(analyze_code_callback, vu=vu))

def function_rename_query(vu, ea, pc):
    
    query_string = '要求：分析下面这段C语言代码，然后为当前函数给出一个更好的函数名。\n' \
                   '重要：回复一个JSON数组，其中键是当前函数的原始名称，值是建议的名称。\n' \
                   '非常重要：只打印JSON字典，不要有任何多余的字、解释、注意事项等：\n' \
                   + pc

    query_model_async(
        query_string,
        functools.partial(function_rename_callback, vu=vu, ea=ea, pc=pc))

def convert_to_python_query():
    vu = hr.get_widget_vdui(kw.get_current_widget())
    if vu == None: return
    
    pseudocode = str(vu.cfunc)
    
    query_string = '分析下面这段C语言代码，然后使用Python实现它的完整功能。\n' \
                   '注意：仅回答python代码即可，不需要任何解释。\n' \
                   '重要：代码必须是```python\ncode\n```这样的格式：\n' \
                   + pseudocode
    
    query_model_async(
            query_string,
            functools.partial(convert_to_python_callback, vu=vu))
            
#子函数重命名模块
class subfunction_rename_t(kw.action_handler_t):

    def __init__(self):
        kw.action_handler_t.__init__(self)
    
    def activate(self, ctx):
        subfunction_rename_query()
        return
        
    def update(self, ctx):
        return kw.AST_ENABLE_FOR_WIDGET if \
            ctx.widget_type == kw.BWN_PSEUDOCODE else \
            kw.AST_DISABLE_FOR_WIDGET
            
#变量重命名模块
class variable_rename_t(kw.action_handler_t):

    def __init__(self):
        kw.action_handler_t.__init__(self)
    
    def activate(self, ctx):
        variable_rename_query()
        return
        
    def update(self, ctx):
        return kw.AST_ENABLE_FOR_WIDGET if \
            ctx.widget_type == kw.BWN_PSEUDOCODE else \
            kw.AST_DISABLE_FOR_WIDGET
            
#添加注释模块
class set_comment_t(kw.action_handler_t):

    def __init__(self):
        kw.action_handler_t.__init__(self)
    
    def activate(self, ctx):
        set_comment_query()
        return
        
    def update(self, ctx):
        return kw.AST_ENABLE_FOR_WIDGET if \
            ctx.widget_type == kw.BWN_PSEUDOCODE else \
            kw.AST_DISABLE_FOR_WIDGET

#分析代码模块
class analyze_code_t(kw.action_handler_t):

    def __init__(self):
        kw.action_handler_t.__init__(self)
    
    def activate(self, ctx):
        analyze_code_query()
        return
        
    def update(self, ctx):
        return kw.AST_ENABLE_FOR_WIDGET if \
            ctx.widget_type == kw.BWN_PSEUDOCODE else \
            kw.AST_DISABLE_FOR_WIDGET
            
#函数重命名模块
class function_rename_t(kw.action_handler_t):

    def __init__(self):
        kw.action_handler_t.__init__(self)
    
    def activate(self, ctx):
        vu = hr.get_widget_vdui(kw.get_current_widget())
        if vu == None: return
        
        function_addr = vu.cfunc.entry_ea
        
        pseudocode = str(vu.cfunc)
        
        function_rename_query(vu, function_addr, pseudocode)
        return
        
    def update(self, ctx):
        return kw.AST_ENABLE_FOR_WIDGET if \
            ctx.widget_type == kw.BWN_PSEUDOCODE else \
            kw.AST_DISABLE_FOR_WIDGET

#转换为Python代码模块
class convert_to_python_t(kw.action_handler_t):

    def __init__(self):
        kw.action_handler_t.__init__(self)
    
    def activate(self, ctx):
        convert_to_python_query()
        return
        
    def update(self, ctx):
        return kw.AST_ENABLE_FOR_WIDGET if \
            ctx.widget_type == kw.BWN_PSEUDOCODE else \
            kw.AST_DISABLE_FOR_WIDGET

PLUGIN_NAME = 'PCodeGPT'

ID_SUBFUNCTION_RENAME = f'{PLUGIN_NAME}:子函数重命名'
ID_VARIABLE_RENAME = f'{PLUGIN_NAME}:变量重命名'
ID_SET_COMMENT = f'{PLUGIN_NAME}:添加注释'
ID_ANALYZE_CODE = f'{PLUGIN_NAME}:分析代码'
ID_FUNCTION_RENAME = f'{PLUGIN_NAME}:函数重命名'
ID_CONVERT_TO_PYTHON = f'{PLUGIN_NAME}:转换为Python代码'

class hexrays_hooks_t(hr.Hexrays_Hooks):

    vu = None

    def __init__(self):
        hr.Hexrays_Hooks.__init__(self)

    def text_ready(self, vu):
        self.vu = vu
        return 0
        
    def _register_action_and_attach_to_popup(self, action_id, action_name, action_class):
        kw.register_action(
            kw.action_desc_t(
                action_id,
                action_name,
                action_class,
                None))
                
        kw.attach_action_to_popup(self.vu.ct, None, action_id, PLUGIN_NAME+"/")
        
        
    def populating_popup(self, widget, popup_handle, vu):
        
        self._register_action_and_attach_to_popup(
            ID_SUBFUNCTION_RENAME,
            "子函数重命名",
            subfunction_rename_t())
            
        self._register_action_and_attach_to_popup(
            ID_VARIABLE_RENAME,
            "变量重命名",
            variable_rename_t())
            
        self._register_action_and_attach_to_popup(
            ID_SET_COMMENT,
            "添加注释",
            set_comment_t())
            
        self._register_action_and_attach_to_popup(
            ID_ANALYZE_CODE,
            "分析代码",
            analyze_code_t())
        
        self._register_action_and_attach_to_popup(
            ID_FUNCTION_RENAME,
            "函数重命名",
            function_rename_t())
            
        self._register_action_and_attach_to_popup(
            ID_CONVERT_TO_PYTHON,
            "转换为Python代码",
            convert_to_python_t())
        
        return 0

class PCodeGPT_t(idaapi.ida_idaapi.plugin_t):

    help = ''
    comment = ''

    wanted_name = PLUGIN_NAME
    wanted_hotkey = ''

    flags = idaapi.PLUGIN_MOD
    
    hr_hexrays_hooks = None

    def init(self):
        if not hr.init_hexrays_plugin():
            return PLUGIN_SKIP
                
        self.hr_hexrays_hooks = hexrays_hooks_t()
        self.hr_hexrays_hooks.hook()
            
        print(f'[+]{PLUGIN_NAME} load')

        return idaapi.PLUGIN_KEEP
        
    def run(self, arg):
        return
        
    def term(self):
        self.hr_hexrays_hooks.unhook()
        del self.hr_hexrays_hooks
        
        print(f'[+]{PLUGIN_NAME} off')
        
        return

def PLUGIN_ENTRY():
    return PCodeGPT_t()
