/* V1.41 filesystem service. Native FS+28 is a same-volume, no-overwrite rename,
 * NOT an atomic replace. All data travels through the native filesystem; this
 * code never writes FAT/FTL/NAND sectors or enables the stock USB disk driver.
 * The original native semaphore serializes each filesystem operation. */
#include "files.h"
#include "debug.h"
#include "firmware.h"
#include <stddef.h>

#define U32(a) (*(volatile uint32_t *)(uintptr_t)(a))
#define FS_TABLE 0x802aa080u
#define FS_SEM 0x8071d378u
#define FS_ERRNO 0x8057aefcu
#define FS_HANDLES 0x80cccb90u
#define FS_NOT_FOUND 0x12u

enum { P_CLOSE_UPLOAD=1,P_OPEN_TEMP,P_READ_TEMP,P_CLOSE_TEMP,P_OPEN_OLD,
       P_READ_OLD,P_BACKUP_OLD,P_INSTALL,P_OPEN_TARGET,P_READ_TARGET,
       P_CLOSE_TARGET,P_FINISH,P_ABORT_CLOSE,P_ABORT_NEW,P_ABORT_RESTORE,
       P_ABORT_TEMP,P_RESTORE_OPEN,P_RESTORE_READ,P_RESTORE_CLOSE };
static uint32_t enabled,state,token,next_token,total,received,expected_crc;
static uint32_t actual_crc,verify_offset,error,fs_error,flags,old_size,old_crc;
static uint32_t last_activity,step_count,phase,crc_running,verify_size;
static uint32_t failure_errno;
static void *handle;
static char target[RD_FILE_PATH_MAX+1],temporary[RD_FILE_PATH_MAX+1];
static char backup[RD_FILE_PATH_MAX+1];
static uint8_t verify_buffer[4096];
/* Native find record owns a 0x10c-byte heap cursor at +0. No filesystem
 * semaphore or file handle remains locked between list commands. */
static uint32_t find_record[0x248/4],find_phase,find_started;
static char find_pattern[RD_FILE_PATH_MAX+5];

static uint32_t get32(const uint8_t *p)
{return p[0]|((uint32_t)p[1]<<8)|((uint32_t)p[2]<<16)|((uint32_t)p[3]<<24);}
static void put32(uint8_t *p,uint32_t v)
{for(unsigned i=0;i<4;++i)p[i]=(uint8_t)(v>>(i*8));}
static unsigned length(const char *p)
{unsigned n=0;while(p[n])++n;return n;}
static uint32_t crc_update(uint32_t crc,const uint8_t *p,unsigned n)
{while(n--){crc^=*p++;for(unsigned b=0;b<8;++b)crc=(crc>>1)^((0u-(crc&1u))&0xedb88320u);}return crc;}
static int ram(uint32_t p,unsigned n)
{return !(p&3u) && p>=0x80004000u && p<0x83c00000u && n<=0x83c00000u-p;}
static uint32_t entry(unsigned offset){return U32(FS_TABLE+offset);}
static void *open_file(const char *path,const char *mode)
{
    void *p=((void *(*)(const char *,const char *))(uintptr_t)entry(0))(path,mode);
    fs_error=U32(FS_ERRNO);return p;
}
static int close_file(void)
{
    void *p=handle;handle=0;
    if(!p)return 0;
    int result=((int (*)(void *))(uintptr_t)entry(4))(p);
    fs_error=U32(FS_ERRNO);return result;
}
static int seek_file(int offset,unsigned whence)
{
    int result=((int (*)(void *,int,unsigned))(uintptr_t)entry(0x10))(handle,offset,whence);
    fs_error=U32(FS_ERRNO);return result;
}
static unsigned read_file(void *p,unsigned n)
{
    unsigned result=((unsigned (*)(void *,unsigned,unsigned,void *))(uintptr_t)entry(8))(p,1,n,handle);
    fs_error=U32(FS_ERRNO);return result;
}
static int remove_file(const char *path)
{
    int result=((int (*)(const char *))(uintptr_t)entry(0x24))(path);
    fs_error=U32(FS_ERRNO);return result;
}
static int rename_file(const char *from,const char *to)
{
    int result=((int (*)(const char *,const char *))(uintptr_t)entry(0x28))(from,to);
    fs_error=U32(FS_ERRNO);return result;
}
static int close_find(void)
{
    if(!find_record[0])return 0;
    int result=((int (*)(void *))(uintptr_t)entry(0x44))(find_record);
    fs_error=U32(FS_ERRNO);
    /* The native wrapper clears +0 only after freeing the allocation. Keep
     * ownership on an error so STOP/cancel can retry through worker poll. */
    return result;
}
static int sem_lock(void)
{
    uint8_t err=255;uint32_t sem=U32(FS_SEM);
    if(!ram(sem,16))return -1;
    ((void (*)(void *,unsigned,uint8_t *))(uintptr_t)0x8000d484u)((void *)(uintptr_t)sem,0,&err);
    return err?-1:0;
}
static void sem_unlock(void)
{((unsigned (*)(void *))(uintptr_t)0x8000d598u)((void *)(uintptr_t)U32(FS_SEM));}
static int stat_file(const char *path,uint32_t out[3])
{
    /* FS+54 does not lock itself. Native stat writes 24 bytes: u16 mode at
     * +2 (0x4000 directory), u32 byte length at +8. Other stdio wrappers
     * already lock, so they MUST NOT run inside this lock. */
    uint32_t st[6]={0};if(sem_lock())return -1;
    int result=((int (*)(const char *,void *))(uintptr_t)entry(0x54))(path,st);
    fs_error=U32(FS_ERRNO);sem_unlock();
    out[0]=out[1]=out[2]=0;
    if(result==0){out[0]=1;out[1]=st[2];out[2]=(st[0]&0x40000000u)?1u:0u;return 0;}
    return fs_error==FS_NOT_FOUND?0:-1;
}
static int other_open(void)
{
    /* Same volume/first-cluster identity as native 80054600, excluding our
     * own reader. Empty-file EOF clusters can conservatively report busy. */
    uint32_t self=(uint32_t)(uintptr_t)handle;
    if(!ram(self,0x58) || sem_lock())return -1;
    unsigned busy=0;
    uint16_t drive=*(uint16_t *)(uintptr_t)(self+0x48);
    uint32_t cluster=U32(self+0x18);
    for(unsigned i=0;i<100;++i){
        uint32_t p=U32(FS_HANDLES+4*i);if(!p || p==self)continue;
        if(!ram(p,0x58)){busy=1;break;}
        if(*(uint16_t *)(uintptr_t)(p+0x48)==drive && U32(p+0x18)==cluster){busy=1;break;}
    }
    sem_unlock();return (int)busy;
}
static int path_copy(char *out,const uint8_t *p,unsigned n,unsigned *parent)
{
    if(n<4 || n>RD_FILE_PATH_MAX || (p[0]!='A' && p[0]!='B' && p[0]!='a' && p[0]!='b') || p[1]!=':' || (p[2]!='\\' && p[2]!='/'))return -1;
    out[0]=(char)(p[0]&~32u);out[1]=':';out[2]='\\';unsigned begin=3,last=3;
    for(unsigned i=3;i<n;){
        unsigned c=p[i];
        if(c>=0x81 && c<=0xfe){
            if(i+1>=n || p[i+1]<0x40 || p[i+1]>0xfe || p[i+1]==0x7f)return -1;
            out[i]=(char)c;out[i+1]=(char)p[i+1];i+=2;continue;
        }
        if(c=='\\' || c=='/'){
            if(i==begin || p[i-1]==' ' || p[i-1]=='.')return -1;
            out[i]='\\';begin=++i;last=begin;continue;
        }
        if(c<32 || c>=0x7f || c==':' || c=='*' || c=='?' || c=='"' || c=='<' || c=='>' || c=='|')return -1;
        out[i++]=(char)c;
    }
    if(begin==n || p[n-1]==' ' || p[n-1]=='.')return -1;
    /* A DBCS trailing 0x5c is never a separator; '.'/' ' cannot be GBK trails. */
    out[n]=0;if(parent)*parent=last;return 0;
}
static int directory_path(char *out,const uint8_t *p,unsigned n)
{
    if(n==3 && (p[0]=='A' || p[0]=='B' || p[0]=='a' || p[0]=='b') &&
       p[1]==':' && (p[2]=='\\' || p[2]=='/')){
        out[0]=(char)(p[0]&~32u);out[1]=':';out[2]='\\';out[3]=0;return 0;
    }
    /* Strip an actual terminal separator, never the 0x5c trail of a GBK
     * character. path_copy remains responsible for all other validation. */
    unsigned i=3,last=0;
    while(i<n){last=i;if(p[i]>=0x81 && p[i]<=0xfe)i+=2;else ++i;}
    if(i==n && last==n-1 && (p[last]=='\\' || p[last]=='/'))--n;
    return path_copy(out,p,n,0);
}
static int path_prefix(const char *a,const char *b)
{
    /* Compare FAT's ASCII case folding without changing DBCS trail bytes.
     * Return true for identical paths or b below directory a. */
    unsigned i=0;
    while(a[i]){
        unsigned x=(uint8_t)a[i],y=(uint8_t)b[i];
        if(x>=0x81 && x<=0xfe){if(x!=y || a[i+1]!=b[i+1])return 0;i+=2;}
        else{if(x>='a' && x<='z')x-=32;if(y>='a' && y<='z')y-=32;if(x!=y)return 0;++i;}
    }
    return !b[i] || b[i]=='\\';
}
static void make_name(char *out,unsigned parent,uint32_t id,const char *ext)
{
    static const char hex[]="0123456789ABCDEF";
    for(unsigned i=0;i<parent;++i)out[i]=target[i];
    out[parent++]='H';out[parent++]='1';out[parent++]='D';
    for(unsigned i=0;i<8;++i)out[parent++]=hex[(id>>(28-4*i))&15u];
    out[parent++]='.';for(unsigned i=0;i<3;++i)out[parent++]=ext[i];out[parent]=0;
}
static int active(void)
{return state==RD_FILE_UPLOAD || state==RD_FILE_VERIFY_TEMP || state==RD_FILE_COMMITTING || state==RD_FILE_VERIFY_TARGET || state==RD_FILE_ABORTING || state==RD_FILE_READING || state==RD_FILE_LISTING;}
int rd_files_busy(void){return active();}
static void recover(void)
{state=RD_FILE_RECOVERY;flags|=RD_FILE_RECOVERY_FLAG;phase=0;}
static void fail(unsigned why)
{if(!error){error=why;failure_errno=fs_error;}state=RD_FILE_ABORTING;flags|=RD_FILE_ABORT_REQUEST;phase=P_ABORT_CLOSE;}
void rd_files_cancel(unsigned why,uint32_t now)
{(void)now;if(active() && state!=RD_FILE_ABORTING)fail(why?why:RD_FILE_CANCELLED);}
static unsigned status_words(uint8_t *out)
{
    const uint32_t values[]={1,state,token,total,received,expected_crc,actual_crc,
        verify_offset,error,error?failure_errno:fs_error,flags,old_size,old_crc,last_activity,step_count,0};
    for(unsigned i=0;i<16;++i)put32(out+4*i,values[i]);return sizeof values;
}
int rd_files_init(void)
{
    static const struct {uint32_t offset,address,first;} entries[]={
        {0,0x8004d028,0x27bdffd8},{4,0x80056c9c,0x27bdffe0},
        {8,0x80056cec,0x27bdffd0},{12,0x80056ea0,0x27bdffd0},
        {16,0x8004d760,0x27bdffd0},{20,0x80056f8c,0x84830048},
        {0x24,0x8004dcb4,0x27bdffd8},{0x28,0x8004e1e0,0x27bdffd0},
        {0x30,0x8004e444,0x27bdffd8},{0x34,0x8004e9d4,0x27bdffd8},
        {0x3c,0x8004eae0,0x27bdffd0},{0x40,0x80057148,0x27bdffd8},
        {0x44,0x80057204,0x27bdffd8},
        {0x54,0x80056524,0x27bdffe0}};
    static const struct {uint32_t address,size,crc;} regions[]={
        {0x8004e1e0,0x198,0x607b7bfe},{0x8004ddec,0x3f4,0xc4c23bdf},
        {0x80056524,0x128,0x053a7887},{0x80056104,0x200,0xa1559b99},
        {0x80054600,0x64,0x0e053b77},{0x800537c4,0x34,0x40d512e8},
        {0x8004bba0,0x3a8,0x2205a461},
        {0x8004e444,0x138,0xb3b0aba9},{0x8004e378,0xcc,0xbac75952},
        {0x8004e57c,0x564,0x4996e7a7},{0x8004eae0,0x124,0x716354b4},
        {0x80057148,0x170,0x7cf18d38},{0x8005bf40,0x1160,0x098fe789},
        {0x8005d6a0,0x120,0x8137f79f}};
    enabled=0;if(rd_control.runtime_tables[1]!=FS_TABLE)return RD_FILE_BINDING;
    for(unsigned i=0;i<sizeof entries/sizeof entries[0];++i)
        if(entry(entries[i].offset)!=entries[i].address || U32(entries[i].address)!=entries[i].first)return RD_FILE_BINDING;
    if(U32(0x8000d484)!=0x3c028058u || U32(0x80054600)!=0x10800010u)return RD_FILE_BINDING;
    for(unsigned i=0;i<sizeof regions/sizeof regions[0];++i)
        if(~crc_update(~0u,(const uint8_t *)(uintptr_t)regions[i].address,regions[i].size)!=regions[i].crc)return RD_FILE_BINDING;
    enabled=1;return 0;
}
static void reset_transaction(uint32_t now)
{
    total=received=expected_crc=actual_crc=verify_offset=error=fs_error=flags=old_size=old_crc=step_count=phase=failure_errno=0;
    state=RD_FILE_IDLE;handle=0;temporary[0]=backup[0]=0;last_activity=now;crc_running=~0u;
    find_phase=find_started=0;
}
static int begin_allowed(void)
{return state==RD_FILE_RECOVERY?RD_FILE_NEEDS_RECOVERY:active()?RD_FILE_BUSY:0;}
static unsigned management_error(uint8_t *out,unsigned *status,unsigned why)
{*status=why;put32(out,fs_error);return 4;}
static unsigned list_next(uint8_t *out,unsigned *status)
{
    while(find_phase<2){
        int result;
        if(!find_started){
            for(unsigned i=0;i<sizeof find_record/sizeof find_record[0];++i)find_record[i]=0;
            result=((int (*)(const char *,unsigned,void *))(uintptr_t)entry(0x3c))(
                find_pattern,find_phase?0x06u:0x16u,find_record);
            find_started=1;
        }else result=((int (*)(void *))(uintptr_t)entry(0x40))(find_record);
        fs_error=U32(FS_ERRNO);
        if(result){
            unsigned saved=fs_error;
            int closed=close_find();
            if(closed || saved!=FS_NOT_FOUND){
                if(!closed)fs_error=saved;
                fail(RD_FILE_IO);return management_error(out,status,RD_FILE_IO);
            }
            ++find_phase;find_started=0;continue;
        }
        const uint8_t *name=(const uint8_t *)find_record+0x12;
        unsigned bytes=0;while(bytes<0x20a && name[bytes])++bytes;
        if(!bytes || bytes>496){fail(RD_FILE_IO);return management_error(out,status,RD_FILE_IO);}
        put32(out,0);put32(out+4,find_record[1]);put32(out+8,find_record[2]);put32(out+12,bytes);
        for(unsigned i=0;i<bytes;++i)out[16+i]=name[i];
        *status=0;return 16+bytes;
    }
    state=RD_FILE_DONE;
    for(unsigned i=0;i<16;++i)out[i]=0;put32(out,1);*status=0;return 16;
}
static unsigned management_command(unsigned op,const uint8_t *p,unsigned n,
                                   uint8_t *out,unsigned *status,uint32_t now)
{
    char path[RD_FILE_PATH_MAX+1],destination[RD_FILE_PATH_MAX+1];uint32_t st[3];
    fs_error=0;
    if(op==61 || op==62){
        if(n!=4)return management_error(out,status,RD_FILE_INVALID);
        if(!token || get32(p)!=token)return management_error(out,status,RD_FILE_TOKEN);
        if(state!=RD_FILE_LISTING && !(state==RD_FILE_DONE && find_phase==2))
            return management_error(out,status,RD_FILE_BUSY);
        last_activity=now;
        if(op==61)return list_next(out,status);
        if(close_find()){fail(RD_FILE_IO);return management_error(out,status,RD_FILE_IO);}
        find_phase=2;state=RD_FILE_DONE;*status=0;return 0;
    }
    unsigned allowed=(unsigned)begin_allowed();
    if(allowed)return management_error(out,status,allowed);
    if(op==60){
        if(directory_path(path,p,n))return management_error(out,status,RD_FILE_INVALID);
        if(stat_file(path,st))return management_error(out,status,RD_FILE_IO);
        if(!st[0])return management_error(out,status,RD_FILE_NOT_FOUND);
        if(!st[2])return management_error(out,status,RD_FILE_NOT_DIRECTORY);
        reset_transaction(now);unsigned bytes=length(path);
        for(unsigned i=0;i<=bytes;++i)target[i]=find_pattern[i]=path[i];
        /* Always add a real separator after a non-root path, including when
         * its final GBK character happens to end in byte 0x5c. */
        if(bytes>3)find_pattern[bytes++]='\\';
        find_pattern[bytes++]='*';find_pattern[bytes++]='.';find_pattern[bytes++]='*';find_pattern[bytes]=0;
        if(++next_token==0)++next_token;token=next_token;state=RD_FILE_LISTING;
        put32(out,token);*status=0;return 4;
    }
    if(op==64){
        if(n<12)return management_error(out,status,RD_FILE_INVALID);
        unsigned bytes=get32(p);
        if(bytes>n-4 || path_copy(path,p+4,bytes,0) || path_copy(destination,p+4+bytes,n-4-bytes,0))
            return management_error(out,status,RD_FILE_INVALID);
        if(path[0]!=destination[0])return management_error(out,status,RD_FILE_CROSS_VOLUME);
    }else if(path_copy(path,p,n,0))return management_error(out,status,RD_FILE_INVALID);
    if(stat_file(path,st))return management_error(out,status,RD_FILE_IO);
    if(op==65){
        if(st[0])return management_error(out,status,RD_FILE_EXISTS);
    }else{
        if(!st[0])return management_error(out,status,RD_FILE_NOT_FOUND);
        if(op==63 && st[2])return management_error(out,status,RD_FILE_IS_DIRECTORY);
        if(op==66 && !st[2])return management_error(out,status,RD_FILE_NOT_DIRECTORY);
        if(op==64){
            if(st[2] && path_prefix(path,destination))return management_error(out,status,RD_FILE_INVALID);
            uint32_t dst[3];
            if(stat_file(destination,dst))return management_error(out,status,RD_FILE_IO);
            if(dst[0])return management_error(out,status,RD_FILE_EXISTS);
        }
        if(!st[2]){
            /* Reject replacing/removing a file still held by the front end.
             * The native operation repeats its own filesystem checks. */
            handle=open_file(path,"rb");
            if(!handle)return management_error(out,status,RD_FILE_IO);
            int busy=other_open();unsigned saved=fs_error;int closed=close_file();
            if(closed)return management_error(out,status,RD_FILE_IO);
            fs_error=saved;
            if(busy)return management_error(out,status,busy<0?RD_FILE_IO:RD_FILE_OPEN);
        }
    }
    int result;
    if(op==63)result=remove_file(path);
    else if(op==64)result=rename_file(path,destination);
    else{
        result=((int (*)(const char *))(uintptr_t)entry(op==65?0x30:0x34))(path);
        fs_error=U32(FS_ERRNO);
    }
    if(result)return management_error(out,status,op==66 && fs_error==0x31?RD_FILE_NOT_EMPTY:RD_FILE_IO);
    *status=0;return 0;
}
static int prepare_upload(const uint8_t *p,unsigned n,uint32_t size,uint32_t crc,uint32_t now)
{
    char path[RD_FILE_PATH_MAX+1];unsigned parent;uint32_t st[3];
    if(size>0x7fffffffu || path_copy(path,p,n,&parent) || parent+15>RD_FILE_PATH_MAX)return RD_FILE_INVALID;
    int result=begin_allowed();if(result)return result;
    if(stat_file(path,st))return RD_FILE_IO;if(st[2])return RD_FILE_INVALID;
    reset_transaction(now);for(unsigned i=0;i<=n;++i)target[i]=path[i];
    total=size;expected_crc=crc;
    for(unsigned attempt=0;attempt<32;++attempt){
        if(++next_token==0)++next_token;token=next_token;
        make_name(temporary,parent,token,"TMP");make_name(backup,parent,token,"BAK");
        if(stat_file(temporary,st)){fail(RD_FILE_IO);return RD_FILE_IO;}if(st[0])continue;
        if(stat_file(backup,st)){fail(RD_FILE_IO);return RD_FILE_IO;}if(st[0])continue;
        handle=open_file(temporary,"wb");
        if(!handle){
            /* fopen can allocate a directory entry before a later allocation
             * or flush fails. Reconcile that previously absent owned name. */
            error=RD_FILE_IO;failure_errno=fs_error;
            if(stat_file(temporary,st)){recover();return RD_FILE_IO;}
            if(st[0])flags|=RD_FILE_TEMP_PRESENT;
            fail(RD_FILE_IO);return RD_FILE_IO;
        }
        flags=RD_FILE_TEMP_PRESENT;state=RD_FILE_UPLOAD;return 0;
    }
    error=RD_FILE_BUSY;state=RD_FILE_FAILED;return RD_FILE_BUSY;
}
static int open_verify(const char *path,uint32_t size)
{
    handle=open_file(path,"rb");if(!handle)return -1;
    if(seek_file(0,2)!=(int)size || seek_file(0,0)!=0)return -1;
    verify_size=size;verify_offset=0;crc_running=~0u;return 0;
}
static int verify_step(void)
{
    unsigned n=verify_size-verify_offset;if(n>sizeof verify_buffer)n=sizeof verify_buffer;
    if(n){if(read_file(verify_buffer,n)!=n)return -1;crc_running=crc_update(crc_running,verify_buffer,n);verify_offset+=n;}
    return verify_offset==verify_size?1:0;
}
void rd_files_poll(uint32_t now)
{
    if(!enabled || !active())return;
    if((state==RD_FILE_UPLOAD || state==RD_FILE_READING || state==RD_FILE_LISTING) && now-last_activity>=RD_FILE_IDLE_TICKS)fail(RD_FILE_EXPIRED);
    if(state==RD_FILE_UPLOAD || state==RD_FILE_READING || state==RD_FILE_LISTING)return;
    ++step_count;uint32_t st[3];int result;
    switch(phase){
    case P_CLOSE_UPLOAD:
        if(close_file()){fail(RD_FILE_IO);break;}phase=P_OPEN_TEMP;break;
    case P_OPEN_TEMP:
        if(open_verify(temporary,total)){fail(RD_FILE_IO);break;}phase=P_READ_TEMP;break;
    case P_READ_TEMP:
        result=verify_step();if(result<0){fail(RD_FILE_IO);break;}
        if(result){actual_crc=~crc_running;if(actual_crc!=expected_crc){fail(RD_FILE_CRC);break;}phase=P_CLOSE_TEMP;}break;
    case P_CLOSE_TEMP:
        if(close_file()){fail(RD_FILE_IO);break;}state=RD_FILE_COMMITTING;phase=P_OPEN_OLD;break;
    case P_OPEN_OLD:
        if(stat_file(target,st) || st[2]){fail(RD_FILE_IO);break;}
        if(!st[0]){phase=P_INSTALL;break;}
        flags|=RD_FILE_OLD_EXISTS;old_size=st[1];
        if(old_size>0x7fffffffu || open_verify(target,old_size)){fail(RD_FILE_IO);break;}
        result=other_open();if(result){fail(result<0?RD_FILE_IO:RD_FILE_OPEN);break;}
        phase=P_READ_OLD;break;
    case P_READ_OLD:
        result=verify_step();if(result<0){fail(RD_FILE_IO);break;}
        if(result){old_crc=~crc_running;phase=P_BACKUP_OLD;}break;
    case P_BACKUP_OLD:
        result=other_open();if(result){fail(result<0?RD_FILE_IO:RD_FILE_OPEN);break;}
        if(close_file()){fail(RD_FILE_IO);break;}
        if(rename_file(target,backup)){
            /* A failed native rename may have reached its final flush.
             * Reconcile only unambiguous states; never delete an unknown target. */
            error=RD_FILE_IO;failure_errno=fs_error;
            if(stat_file(target,st)){recover();break;}unsigned old_present=st[0];
            if(stat_file(backup,st)){recover();break;}
            if(st[0])flags|=RD_FILE_BACKUP_PRESENT;
            if(!old_present && st[0])fail(RD_FILE_IO);
            else if(old_present && !st[0])fail(RD_FILE_IO);else recover();
            break;
        }
        flags|=RD_FILE_BACKUP_PRESENT;phase=P_INSTALL;break;
    case P_INSTALL:
        if(rename_file(temporary,target)){
            error=RD_FILE_IO;failure_errno=fs_error;
            if(stat_file(temporary,st)){recover();break;}
            if(!st[0])flags&=~RD_FILE_TEMP_PRESENT;
            if(stat_file(target,st) || st[0])recover();else fail(RD_FILE_IO);
            break;
        }
        flags=(flags&~RD_FILE_TEMP_PRESENT)|RD_FILE_TARGET_NEW;
        state=RD_FILE_VERIFY_TARGET;phase=P_OPEN_TARGET;break;
    case P_OPEN_TARGET:
        if(open_verify(target,total)){fail(RD_FILE_IO);break;}phase=P_READ_TARGET;break;
    case P_READ_TARGET:
        result=verify_step();if(result<0){fail(RD_FILE_IO);break;}
        if(result){actual_crc=~crc_running;if(actual_crc!=expected_crc){fail(RD_FILE_CRC);break;}phase=P_CLOSE_TARGET;}break;
    case P_CLOSE_TARGET:
        if(close_file()){fail(RD_FILE_IO);break;}phase=P_FINISH;break;
    case P_FINISH:
        /* New content has been closed and read back in full. A failed backup
         * cleanup leaves DONE + BACKUP_PRESENT for the host to report. */
        if((flags&RD_FILE_BACKUP_PRESENT) && !remove_file(backup))flags&=~RD_FILE_BACKUP_PRESENT;
        state=RD_FILE_DONE;phase=0;break;
    case P_ABORT_CLOSE:
        if(find_record[0] && close_find())break;
        if(close_file() && !error)error=RD_FILE_IO;phase=P_ABORT_NEW;break;
    case P_ABORT_NEW:
        if(flags&RD_FILE_TARGET_NEW){
            if(remove_file(target)){recover();break;}flags&=~RD_FILE_TARGET_NEW;
        }
        phase=P_ABORT_RESTORE;break;
    case P_ABORT_RESTORE:
        if(flags&RD_FILE_BACKUP_PRESENT){
            if(rename_file(backup,target)){recover();break;}flags&=~RD_FILE_BACKUP_PRESENT;
            phase=P_RESTORE_OPEN;break;
        }
        phase=P_ABORT_TEMP;break;
    case P_RESTORE_OPEN:
        if(open_verify(target,old_size)){(void)close_file();recover();break;}
        phase=P_RESTORE_READ;break;
    case P_RESTORE_READ:
        result=verify_step();
        if(result<0 || (result && ~crc_running!=old_crc)){(void)close_file();recover();break;}
        if(result)phase=P_RESTORE_CLOSE;break;
    case P_RESTORE_CLOSE:
        if(close_file()){recover();break;}phase=P_ABORT_TEMP;break;
    case P_ABORT_TEMP:
        if(flags&RD_FILE_TEMP_PRESENT){
            if(remove_file(temporary)){recover();break;}flags&=~RD_FILE_TEMP_PRESENT;
        }
        state=(error==RD_FILE_CANCELLED || error==RD_FILE_TRANSPORT || error==RD_FILE_EXPIRED)?RD_FILE_ABORTED:RD_FILE_FAILED;
        phase=0;break;
    default:fail(RD_FILE_IO);break;
    }
}
unsigned rd_files_command(unsigned op,const uint8_t *p,unsigned n,uint8_t *out,unsigned *status)
{
    *status=RD_FILE_INVALID;uint32_t now=fw_tick();
    if(op==40){if(n)return 0;*status=0;return status_words(out);}
    if(op==49){
        if(n!=8 || get32(p)!=token || !token || get32(p+4)>2)return 0;
        const char *s=get32(p+4)==0?target:get32(p+4)==1?temporary:backup;
        unsigned count=length(s);for(unsigned i=0;i<count;++i)out[i]=(uint8_t)s[i];*status=0;return count;
    }
    if(!enabled){*status=RD_FILE_BINDING;return 0;}
    if(op>=60 && op<=66)return management_command(op,p,n,out,status,now);
    if(op==41){
        if(n<12)return 0;
        *status=(unsigned)prepare_upload(p+8,n-8,get32(p),get32(p+4),now);
        if(*status)return 0;put32(out,token);put32(out+4,0);return 8;
    }
    if(op==48){
        char path[RD_FILE_PATH_MAX+1];uint32_t st[3];
        if(directory_path(path,p,n))return 0;
        if(stat_file(path,st)){*status=RD_FILE_IO;return 0;}
        for(unsigned i=0;i<3;++i)put32(out+4*i,st[i]);*status=0;return 12;
    }
    if(op==45){
        char path[RD_FILE_PATH_MAX+1];uint32_t st[3];
        if(path_copy(path,p,n,0))return 0;
        *status=(unsigned)begin_allowed();if(*status)return 0;
        if(stat_file(path,st)){*status=RD_FILE_IO;return 0;}
        if(!st[0]){*status=RD_FILE_NOT_FOUND;return 0;}
        if(st[2] || st[1]>0x7fffffffu){*status=RD_FILE_INVALID;return 0;}
        reset_transaction(now);for(unsigned i=0;i<=n;++i)target[i]=path[i];
        if(++next_token==0)++next_token;token=next_token;total=st[1];
        handle=open_file(target,"rb");if(!handle){fail(RD_FILE_IO);*status=RD_FILE_IO;return 0;}
        state=RD_FILE_READING;
        put32(out,token);put32(out+4,total);*status=0;return 8;
    }
    if(n<4)return 0;
    if(get32(p)!=token || !token){*status=RD_FILE_TOKEN;return 0;}
    last_activity=now;
    switch(op){
    case 42:
        if(n<9 || n>512)return 0;
        if(state!=RD_FILE_UPLOAD){*status=RD_FILE_BUSY;return 0;}
        if(get32(p+4)!=received){*status=RD_FILE_OFFSET;return 0;}
        if(n-8>total-received)return 0;
        unsigned count=((unsigned (*)(const void *,unsigned,unsigned,void *))(uintptr_t)entry(12))(p+8,1,n-8,handle);
        fs_error=U32(FS_ERRNO);
        if(count>n-8){fail(RD_FILE_IO);*status=RD_FILE_IO;return 0;}
        crc_running=crc_update(crc_running,p+8,count);received+=count;
        if(count!=n-8){fail(RD_FILE_IO);*status=RD_FILE_IO;return 0;}
        put32(out,received);*status=0;return 4;
    case 43:
        if(n!=4)return 0;
        if(state!=RD_FILE_UPLOAD){*status=RD_FILE_BUSY;return 0;}
        if(received!=total){*status=RD_FILE_OFFSET;return 0;}
        if(~crc_running!=expected_crc){fail(RD_FILE_CRC);*status=RD_FILE_CRC;return 0;}
        state=RD_FILE_VERIFY_TEMP;phase=P_CLOSE_UPLOAD;*status=0;return 0;
    case 44:
        if(n!=4)return 0;
        rd_files_cancel(RD_FILE_CANCELLED,now);*status=0;return 0;
    case 46:
        if(n!=12)return 0;
        if(state!=RD_FILE_READING){*status=RD_FILE_BUSY;return 0;}
        unsigned offset=get32(p+4),wanted=get32(p+8);
        if(offset>total || !wanted || wanted>512)return 0;
        if(wanted>total-offset)wanted=total-offset;
        if(seek_file((int)offset,0)!=(int)offset){fail(RD_FILE_IO);*status=RD_FILE_IO;return 0;}
        if(read_file(out,wanted)!=wanted){fail(RD_FILE_IO);*status=RD_FILE_IO;return 0;}
        received=offset+wanted;*status=0;return wanted;
    case 47:
        if(n!=4)return 0;
        if(state!=RD_FILE_READING){*status=RD_FILE_BUSY;return 0;}
        if(close_file()){error=RD_FILE_IO;failure_errno=fs_error;state=RD_FILE_FAILED;*status=RD_FILE_IO;return 0;}
        state=RD_FILE_DONE;*status=0;return 0;
    default:return 0;
    }
}
