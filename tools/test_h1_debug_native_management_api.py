"""Native directory API proof in the existing private 8857/8858 H1 image."""
from pathlib import Path
import hashlib,json,struct
from test_h1_debug_native_deploy import TaskCall,NativeClient,nav,KERNEL_SHA,ROOT

def main():
    work=ROOT/'build/h1-debug-native-files-probe'
    state=nav.api('http://127.0.0.1:8857','/api/status')
    assert Path(state['nand']).resolve()==work/'h1-system.raw'
    assert Path(state['kernel']).resolve()==work/'project.bin'
    assert hashlib.sha256((work/'project.bin').read_bytes()).hexdigest()==KERNEL_SHA
    build=json.loads((work/'tested-build.json').read_text(encoding='utf-8'))
    client=NativeClient(8858,build)
    root=r'A:\H1MGTEST';renamed=r'A:\H1MGREN'
    assert not client.file_stat(root)['exists'] and not client.file_stat(renamed)['exists']
    with TaskCall(8858,build) as task:
        fs=struct.unpack('<24I',task.r.memory(0x802aa080,96));base=task.buffer
        def path(value,slot=0):
            address=base+slot*256;task.write(address,value.encode('gbk')+b'\0');return address
        def action(index,*args):return task.call(fs[index],*args)
        assert action(12,path(root))==0
        assert action(12,path(root+r'\子目录'))==0
        handle=action(0,path(root+r'\测试.txt'),path('wb',1));assert handle
        task.write(base+512,b'H1 directory management proof')
        assert action(3,base+512,1,29,handle)==29
        assert action(1,handle)==0
        record=base+2048;found=[]
        for mask in (0x16,0x06):
            task.write(record,bytes(0x248));rc=action(15,path(root+r'\*.*'),mask,record)
            try:
                for _ in range(64):
                    if rc==0xffffffff:break
                    assert rc==0
                    raw=task.r.memory(record,0x248)
                    name=raw[0x12:].split(b'\0')[0].decode('gbk')
                    found.append(dict(mask=mask,name=name,size=struct.unpack_from('<I',raw,4)[0],
                                      attrs=raw[8],record_head=raw[:18].hex()))
                    rc=action(16,record)
                else:raise AssertionError('Enumeration did not terminate')
                assert task.word(0x8057aefc)==0x12
            finally:
                if task.word(record):assert action(17,record)==0
        assert any(r['name']=='测试.txt' and r['size']==29 for r in found),found
        assert any(r['name']=='子目录' and r['attrs']&0x10 for r in found),found
        assert action(13,path(root))==0xffffffff
        nonempty_errno=task.word(0x8057aefc);assert nonempty_errno==0x31
        assert action(10,path(root),path(renamed,1))==0
        assert action(9,path(renamed+r'\测试.txt'))==0
        assert action(13,path(renamed+r'\子目录'))==0
        assert action(13,path(renamed))==0
    assert not client.file_stat(root)['exists'] and not client.file_stat(renamed)['exists']
    report=dict(ok=True,firmware_sha256=KERNEL_SHA,physical_hardware_tested=False,
                usb_transport_tested=False,worker_priority=6,entries=found,
                nonempty_rmdir_refused=True,nonempty_errno=nonempty_errno,
                directory_rename_and_child_resolution=True,created_artifacts_removed=True)
    (work/'native-management-api-verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
