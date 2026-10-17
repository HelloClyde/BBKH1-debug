/* H1/Y100 V1.41, project.bin SHA256
 * d05786e442f9aad62a8d0a0cb4f6d786bdc7c2fa353a7a2b152c9ed9f01b40ef.
 * DispatchMessage 80114528 reads HWND+88. We replace only the desktop's
 * callback pointer, never a half-executed function prologue. Existing timer
 * messages (144) supply a GUI-task safe point. Native PostMessage's producer
 * ring is not locked, so the worker deliberately does not post into it.
 * Normal loader 80077A84 owns validation, loading and the 83C00020 call.
 * Its single jalr instruction is redirected solely to observe actual entry.
 */
#include "shell.h"
#include "debug.h"
#include "firmware.h"

#define U32(a) (*(volatile uint32_t *)(uintptr_t)(a))
#define U16(a) (*(volatile uint16_t *)(uintptr_t)(a))
#define U8(a) (*(volatile uint8_t *)(uintptr_t)(a))
#define ROOT_WINDOW 0x80477658u
#define DESKTOP_PROC 0x80083c20u
#define DESKTOP_BUSY 0x80859a9cu
#define DESKTOP_SKIN 0x808599e0u
#define APP_COUNT 0x80859a84u
#define APP_TABLE 0x80ccd050u
#define APP_STRIDE 344u
#define LOADER_CALL 0x80077c64u
#define LOADER_JALR 0x0040f809u
#define CURRENT_TCB 0x8057adfcu
#define FS_TABLE 0x802aa080u
#define QUEUE_TICKS 200u /* The V1.41 raw clock is 40 Hz: five seconds. */

typedef int (*window_proc)(uint32_t,uint32_t,uint32_t,uint32_t);
static uint32_t installed,window,entry_jump,next_job,queued_tick,entry_start;
static volatile uint32_t job,kind,state,error,app_index,entries,return_code,callback_active;
static char path[RD_SHELL_PATH_MAX+1];
static const uint8_t directory[]={ 'A',':','\\',0xd3,0xa6,0xd3,0xc3,'\\',0xb3,0xcc,0xd0,0xf2,'\\',0 };

static uint32_t lock(void)
{
    uint32_t old,disabled;
    __asm__ volatile("mfc0 %0,$12\n\tnop":"=r"(old));disabled=old&~1u;
    __asm__ volatile("mtc0 %0,$12\n\tnop\n\tnop\n\tnop"::"r"(disabled):"memory");return old;
}
static void unlock(uint32_t old)
{__asm__ volatile("mtc0 %0,$12\n\tnop\n\tnop\n\tnop"::"r"(old):"memory");}
static int pointer(uint32_t p,uint32_t n)
{return !(p&3u) && p>=0x80004000u && p<0x83c00000u && n<=0x83c00000u-p;}
static int gui_task(void)
{
    uint32_t tcb=U32(CURRENT_TCB);
    return pointer(tcb,0x40) && U8(tcb+0x35)==10;
}
static int window_valid(uint32_t p)
{return pointer(p,0xf4) && U16(p)==1 && U16(p+2)==0x11;}
static int menu_ready(void)
{
    uint32_t skin=U32(DESKTOP_SKIN);
    return window_valid(window) && U32(ROOT_WINDOW)==window &&
        /* GetActiveWindow: F4 handler 8010E2F8 returns 8057B020; its
         * wrapper 801045F0 selects focused child +D8 when nonzero. */
        U32(0x8057b020u)==window && !U32(window+0xd8) &&
        !U32(DESKTOP_BUSY) && !U32(0x80859b78u) && skin>=2 && skin<=4;
}
int rd_shell_active(void)
{return callback_active || state==RD_SHELL_QUEUED || state==RD_SHELL_LOADING || state==RD_SHELL_RUNNING;}
int rd_shell_stop_blocked(void)
{return state==RD_SHELL_QUEUED || state==RD_SHELL_LOADING;}

static int firmware_matches(void)
{
    static const uint32_t signatures[][3]={
        {DESKTOP_PROC,0x27bdffd8u,0x24020144u},
        {0x80114528u,0x27bdffe8u,0xafbf0010u},
        {0x8007766cu,0x27bdfde8u,0xafb00210u},
        {0x80077a84u,0x27bdfee0u,0x3c058030u},
        {0x800781e0u,0x27bdf810u,0x24063310u},
        {0x80078968u,0x3c028086u,0x8c429a3cu},
        {0x80084cb4u,0x27bdffc8u,0xafb7002cu},
        {0x80085324u,0x27bdffd8u,0xafb40020u},
        {0x8010e2f8u,0x3c048058u,0x8c84b020u},
        {0x80103c4cu,0x27bdffe0u,0xafbf0018u},
        {0x8004d028u,0x27bdffd8u,0xafb00018u},
        {0x80056c9cu,0x27bdffe0u,0xafb00018u},
        {0x80056cecu,0x27bdffd0u,0xafb40028u},
        {0x8004d760u,0x27bdffd0u,0xafb40028u},
        {0x80056f8cu,0x84830048u,0x04600006u},
    };
    for(unsigned i=0;i<sizeof(signatures)/sizeof(signatures[0]);++i)
        if(U32(signatures[i][0])!=signatures[i][1] || U32(signatures[i][0]+4)!=signatures[i][2])return 0;
    return U32(0x80114554u)==0x8c620088u && U32(0x80077c5cu)==0x3c0283c0u &&
        U32(0x80077c60u)==0x34420020u && U32(LOADER_CALL+4)==0 &&
        U32(FS_TABLE)==0x8004d028u && U32(FS_TABLE+4)==0x80056c9cu &&
        U32(FS_TABLE+8)==0x80056cecu && U32(FS_TABLE+0x10)==0x8004d760u &&
        U32(FS_TABLE+0x14)==0x80056f8cu;
}
static int desktop_callback(uint32_t hwnd,uint32_t msg,uint32_t a,uint32_t b);
/* jal at the loader preserves its original return address. The bridge uses
 * only a caller-saved argument register; C preserves the loader's s0..s7/gp.
 * The original entry is in v0, not an ABI argument. The delay slot transfers
 * it before the C function prologue can reuse v0. */
extern int rd_shell_entry_bridge(void);
__asm__(".set push\n.set noreorder\n.text\n.align 2\n"
        ".globl rd_shell_entry_bridge\nrd_shell_entry_bridge:\n"
        "j rd_shell_application_entry\nmove $4,$2\n.set pop\n");
int rd_shell_application_entry(uint32_t target) __attribute__((used,noinline));
int rd_shell_application_entry(uint32_t target)
{
    int ours=callback_active && kind==RD_SHELL_RUN && state==RD_SHELL_LOADING && gui_task();
    if(ours){++entries;state=RD_SHELL_RUNNING;}
    int result=((int (*)(void))(uintptr_t)target)();
    if(ours)return_code=(uint32_t)result;
    return result;
}
int rd_shell_check(void)
{
    return installed && window_valid(window) && U32(ROOT_WINDOW)==window &&
        U32(window+0x88)==(uint32_t)(uintptr_t)desktop_callback &&
        U32(LOADER_CALL)==entry_jump && U32(LOADER_CALL+4)==0 ? 0:RD_SHELL_BINDING;
}
int rd_shell_init(void)
{
    if(installed)return rd_shell_check();
    if(!firmware_matches())return RD_SHELL_BINDING;
    uint32_t flags=lock(),candidate=U32(ROOT_WINDOW);
    if(!window_valid(candidate) || U32(candidate+0x88)!=DESKTOP_PROC || U32(LOADER_CALL)!=LOADER_JALR){
        unlock(flags);return RD_SHELL_BINDING;
    }
    window=candidate;entry_jump=0x0c000000u|(((uint32_t)(uintptr_t)rd_shell_entry_bridge>>2)&0x03ffffffu);
    U32(LOADER_CALL)=entry_jump;
    rd_cache_sync((void *)(uintptr_t)LOADER_CALL,(void *)(uintptr_t)(LOADER_CALL+4));
    U32(window+0x88)=(uint32_t)(uintptr_t)desktop_callback;
    installed=1;unlock(flags);return 0;
}
int rd_shell_shutdown(void)
{
    uint32_t flags=lock();
    if(!installed){unlock(flags);return 0;}
    if(rd_shell_stop_blocked()){unlock(flags);return RD_SHELL_BUSY;}
    if(rd_shell_check()){unlock(flags);return RD_SHELL_BINDING;}
    /* RUNNING already entered the payload. Detach only future dispatches;
     * its live return chain remains in the permanently retained resident
     * allocation. In particular, a remote launch of this same debugger can
     * stop its worker without waiting for its own launcher BDA to return.
     * Keep callback_active/job/state intact until desktop_callback returns. */
    U32(window+0x88)=DESKTOP_PROC;U32(LOADER_CALL)=LOADER_JALR;
    rd_cache_sync((void *)(uintptr_t)LOADER_CALL,(void *)(uintptr_t)(LOADER_CALL+4));
    installed=0;unlock(flags);return 0;
}
static unsigned lower(unsigned c){return c>='A'&&c<='Z'?c+32:c;}
static int valid_path(const uint8_t *p,unsigned n)
{
    unsigned prefix=sizeof(directory)-1;
    if(!p || n<=prefix+4 || n>RD_SHELL_PATH_MAX)return 0;
    for(unsigned i=0;i<prefix;++i)if(p[i]!=directory[i])return 0;
    if(p[prefix]=='.' || p[n-1]==' ' || p[n-1]=='.')return 0;
    for(unsigned i=prefix;i<n;++i){
        unsigned c=p[i];
        if(c>=0x81 && c<=0xfe){
            if(++i>=n || p[i]<0x40 || p[i]>0xfe || p[i]==0x7f)return 0;
        }else if(c<0x20 || c>=0x7f || c=='\\' || c=='/' || c==':' ||
                 c=='*' || c=='?' || c=='"' || c=='<' || c=='>' || c=='|')return 0;
    }
    return p[n-4]=='.' && lower(p[n-3])=='b' && lower(p[n-2])=='d' && lower(p[n-1])=='a';
}
int rd_shell_request(uint32_t requested,const uint8_t *p,uint32_t n,uint32_t *id)
{
    if(!id || (requested!=RD_SHELL_REFRESH && requested!=RD_SHELL_RUN) ||
       (requested==RD_SHELL_REFRESH ? n!=0:!valid_path(p,n)))return RD_SHELL_INVALID;
    uint32_t flags=lock();
    if(rd_shell_check()){unlock(flags);return RD_SHELL_BINDING;}
    if(rd_shell_active() || !menu_ready()){unlock(flags);return RD_SHELL_BUSY;}
    for(unsigned i=0;i<n;++i)path[i]=(char)p[i];path[n]=0;
    if(++next_job==0)++next_job;
    job=next_job;kind=requested;error=0;entry_start=entries;return_code=0;app_index=0xffffffffu;
    queued_tick=fw_tick();state=RD_SHELL_QUEUED;*id=job;
    unlock(flags);return 0;
}
void rd_shell_poll(uint32_t now)
{
    uint32_t flags=lock();
    if(state==RD_SHELL_QUEUED){
        unsigned failure=rd_shell_check()?RD_SHELL_BINDING:
            !menu_ready()?RD_SHELL_BUSY:(uint32_t)(now-queued_tick)>=QUEUE_TICKS?RD_SHELL_EXPIRED:0;
        if(failure){error=failure;state=RD_SHELL_FAILED;}
    }
    unlock(flags);
}
void rd_shell_status(uint32_t out[8])
{
    uint32_t flags=lock();
    out[0]=job;out[1]=kind;out[2]=state;out[3]=error;
    out[4]=installed && !rd_shell_check() && !rd_shell_active() && menu_ready();
    out[5]=app_index;out[6]=entries;out[7]=return_code;
    unlock(flags);
}
static void refresh(void)
{
    ((void (*)(const char *))(uintptr_t)0x800781e0u)((const char *)directory);
    ((void (*)(void))(uintptr_t)0x80078968u)();
    ((void (*)(void))(uintptr_t)0x80084cb4u)();
    ((void (*)(void))(uintptr_t)0x80085324u)();
}
static void redraw(void)
{
    U32(0x80859b6cu)=1;U32(0x80859b70u)=1;
    ((int (*)(uint32_t,uint32_t,uint32_t))(uintptr_t)0x80103c4cu)(window,0,0);
}
static int filename_equal(const uint8_t *a,const uint8_t *b)
{
    unsigned i=0;
    while(i<256 && a[i] && b[i]){
        if(a[i]>=0x81 && a[i]<=0xfe){
            if(a[i]!=b[i] || i+1>=256 || a[i+1]!=b[i+1])return 0;
            i+=2;
        }else{
            if(lower(a[i])!=lower(b[i]))return 0;
            ++i;
        }
    }
    return i<256 && !a[i] && !b[i];
}
static unsigned find_app(void)
{
    uint32_t count=U32(APP_COUNT);
    if(count>150)return 0xffffffffu;
    const uint8_t *wanted=(const uint8_t *)path+sizeof(directory)-1;
    for(unsigned i=0;i<count;++i){
        uintptr_t record=APP_TABLE+i*APP_STRIDE;
        if(U32(record+0x2c))continue;
        const uint8_t *name=(const uint8_t *)(record+0x30);
        if(filename_equal(wanted,name))return i;
    }
    return 0xffffffffu;
}
static unsigned validate_bda(void)
{
    uint32_t header[34];
    void *handle=((void *(*)(const char *,const char *))(uintptr_t)U32(FS_TABLE))(path,"rb");
    if(!handle)return RD_SHELL_NOT_FOUND;
    unsigned read=((unsigned (*)(void *,unsigned,unsigned,void *))(uintptr_t)U32(FS_TABLE+8))(header,1,sizeof(header),handle);
    int seek=((int (*)(void *,int,unsigned))(uintptr_t)U32(FS_TABLE+0x10))(handle,0,2);
    int size=((int (*)(void *))(uintptr_t)U32(FS_TABLE+0x14))(handle);
    int closed=((int (*)(void *))(uintptr_t)U32(FS_TABLE+4))(handle);
    /* Native fseek returns the resulting absolute offset, not ISO C's zero. */
    if(read!=sizeof(header) || seek<0 || seek!=size || closed || size<0)return RD_SHELL_IO;
    for(unsigned i=0;i<11;++i)header[i]^=0x44525744u;
    unsigned checksum=0;const uint8_t *bytes=(const uint8_t *)header;
    for(unsigned i=0;i<0x84;++i)checksum+=bytes[i];
    if(header[0]!=0x004b4242u || header[1]!=0x5d245562u || header[2]!=0x01000102u ||
       header[33]!=(checksum^0x322d464bu) || size<0x8c || (unsigned)size>0x400000u ||
       header[4]!=(unsigned)size-4 || header[5]<0x88 || header[5]>=(unsigned)size ||
       (header[5]&3u) || header[6]!=0x88)return RD_SHELL_BDA_INVALID;
    return 0;
}
static int desktop_callback(uint32_t hwnd,uint32_t msg,uint32_t a,uint32_t b)
{
    int result=((window_proc)(uintptr_t)DESKTOP_PROC)(hwnd,msg,a,b);
    if(msg!=0x144u || hwnd!=window || state!=RD_SHELL_QUEUED)return result;
    uint32_t flags=lock();
    if(state!=RD_SHELL_QUEUED){unlock(flags);return result;}
    if(rd_shell_check() || !menu_ready() || !gui_task()){
        error=RD_SHELL_CONTEXT;state=RD_SHELL_FAILED;unlock(flags);return result;
    }
    callback_active=1;state=RD_SHELL_LOADING;U32(DESKTOP_BUSY)=1;unlock(flags);
    unsigned failure=0;
    if(kind==RD_SHELL_REFRESH)refresh();
    else{
        failure=validate_bda();
        if(!failure){
            app_index=find_app();
            if(app_index==0xffffffffu)failure=RD_SHELL_NOT_FOUND;
            else{
                ((int (*)(unsigned))(uintptr_t)0x8007766cu)(app_index);
                if(entries==entry_start)failure=RD_SHELL_LOAD_FAILED;
            }
        }
    }
    U32(DESKTOP_BUSY)=0;redraw();
    flags=lock();error=failure;state=failure?RD_SHELL_FAILED:RD_SHELL_DONE;callback_active=0;unlock(flags);
    return result;
}
