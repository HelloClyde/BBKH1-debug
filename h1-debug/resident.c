/* USB debugger task. All state and all code live in one relocated global-heap
 * allocation; nothing here relies on the next BDA's 0x83c00000 service prefix. */
#include "debug.h"
#include "firmware.h"
#include "io.h"
#include "usb.h"
#include "screen_codec.h"
#include "files.h"
#include "shell.h"
#include "../experiments/h1-usb-debug/protocol.h"
#include "../experiments/h1-usb-runner/native_exec.h"
#include <stddef.h>

static int resident_init(rd_header *);
static void resident_worker(void *);
static void resident_shutdown(rd_header *);
static uint8_t task_stack[RD_STACK_BYTES] __attribute__((aligned(32)));
rd_header rd_control __attribute__((section(".resident_header"))) = {
    .magic=RD_MAGIC,.version=RD_VERSION,.priority=RD_PRIORITY,
    .init=resident_init,.worker=resident_worker,.shutdown=resident_shutdown,
    .stack_top=(uint32_t)(uintptr_t)(task_stack+RD_STACK_BYTES-4),
    .stack_bottom=(uint32_t)(uintptr_t)task_stack,.image_id=RD_BUILD_ID
};
static debug_usb usb;
static debug_protocol protocol;
static uint8_t screen[480u*272u*4u] __attribute__((aligned(32)));
static uint32_t screen_meta[6],screen_token,screen_tick,screen_valid;
static uint32_t commands,polls,last_poll,reset_seen,cable_seen,configured_seen,session_seen,zlp_pending,stop_tick;
static uint32_t completed_stops,last_stop_error,last_stop_tick;
static int in_poll;
/* Readable with RAM_READ and build symbols; no wire/status ABI change. */
static volatile uint32_t service_calls,active_windows,window_polls_max,frame_limit_exits;
static volatile uint32_t iteration_limit_exits,tick_limit_exits,idle_returns;
enum { TRANSPORT_PROGRESS=1u,TRANSPORT_RESTART=2u,TRANSPORT_MAX_POLLS=4096u };

void *memset(void *dst,int value,size_t n)
{uint8_t *p=dst;while(n--)*p++=(uint8_t)value;return dst;}
void *memcpy(void *dst,const void *src,size_t n)
{uint8_t *d=dst;const uint8_t *s=src;while(n--)*d++=*s++;return dst;}
void rd_cache_sync(void *start,void *end)
{
    uintptr_t a=(uintptr_t)start&~15u,b=((uintptr_t)end+15u)&~15u;
    __asm__ volatile("sync" ::: "memory");
    for(uintptr_t p=a;p<b;p+=16u)__asm__ volatile("cache 0x15,0(%0)"::"r"(p):"memory");
    __asm__ volatile("sync" ::: "memory");
    for(uintptr_t p=a;p<b;p+=16u)__asm__ volatile("cache 0x10,0(%0)"::"r"(p):"memory");
    __asm__ volatile("sync\n\tnop\n\tnop\n\tnop" ::: "memory");
}
static uint32_t get32(const uint8_t *p)
{return p[0]|((uint32_t)p[1]<<8)|((uint32_t)p[2]<<16)|((uint32_t)p[3]<<24);}
static void put32(uint8_t *p,uint32_t x)
{for(unsigned j=0;j<4;++j)p[j]=(uint8_t)(x>>(8*j));}
static unsigned words(uint8_t *out,const uint32_t *v,unsigned n)
{for(unsigned j=0;j<n;++j)put32(out+4*j,v[j]);return 4*n;}
static int ram_range(uint32_t a,uint32_t n)
{return n && a>=0x80000000u && a<0x84000000u && n<=0x84000000u-a;}
static int intersects(uint32_t a,uint32_t n,uint32_t b,uint32_t m)
{return a<b+m && b<a+n;}
static unsigned dispatch(unsigned op,const uint8_t *p,unsigned n,uint8_t *out,unsigned *status)
{
    *status=0;++commands;
    if(exec_active() && op!=1 && op!=2 && op!=3 && op!=9 && op!=10 && op!=12 && op!=14 && op!=26 && op!=31 && op!=32 && op!=40 && op!=49 && op!=52){*status=2;return 0;}
    if((op==27 || op==30 || op==50 || op==51) && rd_files_busy()){*status=2;return 0;}
    if(op==27 || op==30 || op==41 || op==43 || op==45 || (op>=60 && op<=66)){
        uint32_t shell[8];rd_shell_status(shell);
        if(shell[2]==1u || shell[2]==2u){*status=2;return 0;}
    }
    if((op>=40 && op<=49) || (op>=60 && op<=66))return rd_files_command(op,p,n,out,status);
    switch(op){
    case 1: {
        static const char identity[]="H1DEBUG/1 V1.41 RESIDENT";
        if(n)break;memcpy(out,identity,sizeof(identity)-1);return sizeof(identity)-1;
    }
    case 2: {
        if(n)break;uint32_t lo,hi;rd_keys_get(&lo,&hi);
        uint32_t v[]={1,rd_control.state,usb.configured,usb.ownership_fault,usb.resets,
            commands,protocol.bad_frames,fw_tick(),polls,last_poll,rd_control.base,rd_control.size,
            lo,hi,exec_diag.state,rd_control.last_error};return words(out,v,16);
    }
    case 3: {
        if(n!=8)break;uint32_t a=get32(p),count=get32(p+4);
        if(count>512 || !ram_range(a,count))break;
        const volatile uint8_t *src=(const volatile uint8_t *)(uintptr_t)a;
        for(unsigned i=0;i<count;++i)out[i]=src[i];return count;
    }
    case 4: {
        if(n<5)break;uint32_t a=get32(p),count=n-4;
        if(!ram_range(a,count) || intersects(a,count,rd_control.allocation,rd_control.size+31u))break;
        volatile uint8_t *dst=(volatile uint8_t *)(uintptr_t)a;
        for(unsigned i=0;i<count;++i)dst[i]=p[4+i];
        rd_cache_sync((void *)(uintptr_t)a,(void *)(uintptr_t)(a+count));return 0;
    }
    case 5:
        if(n!=12)break;
        *status=(unsigned)rd_keys_set(get32(p),get32(p+4),get32(p+8),fw_tick());return 0;
    case 6: {
        if(n)break;screen_valid=0;
        int result=rd_screen_capture(screen,sizeof screen,screen_meta);
        if(result){*status=(unsigned)result;return 0;}
        if(++screen_token==0)++screen_token;screen_tick=fw_tick();screen_valid=1;
        put32(out,screen_token);words(out+4,screen_meta,6);put32(out+28,screen_tick);return 32;
    }
    case 7: {
        if(n!=12)break;uint32_t token=get32(p),offset=get32(p+4),wanted=get32(p+8);
        if(!screen_valid || token!=screen_token){*status=2;return 0;}
        if(!wanted || wanted>500 || offset>=screen_meta[4])break;
        uint32_t count=screen_meta[4]-offset;if(count>wanted)count=wanted;
        put32(out,token);put32(out+4,offset);put32(out+8,screen_meta[4]);
        memcpy(out+12,screen+offset,count);return 12+count;
    }
    case 8:
        if(n!=4)break;if(!screen_valid || get32(p)!=screen_token){*status=2;return 0;}
        screen_valid=0;return 0;
    case 9: {
        if(n)break;uint32_t v[]={1,0x3fff,512,0x80000000u,0x04000000u,1,0,0};return words(out,v,8);
    }
    case 10:
        if(n)break;if(rd_shell_stop_blocked()){*status=2;return 0;}
        rd_control.stop_request=1;stop_tick=fw_tick();exec_request_cancel(EXEC_USER_CANCEL);
        rd_files_cancel(RD_FILE_CANCELLED,stop_tick);return 0;
    case 11:
        if(n!=16)break;
        *status=(unsigned)rd_touch_set(get32(p),get32(p+4),get32(p+8),get32(p+12),fw_tick());return 0;
    case 12: {
        if(n)break;uint32_t v[4];rd_touch_get(v,fw_tick());return words(out,v,4);
    }
    case 13: {
        if(n!=12)break;uint32_t token=get32(p),offset=get32(p+4),wanted=get32(p+8);
        if(!screen_valid || token!=screen_token){*status=2;return 0;}
        unsigned bytes=screen_meta[3]==1u?2u:4u;
        if(!wanted || wanted>16384u || offset>=screen_meta[4] ||
           offset%bytes || wanted%bytes)break;
        unsigned count=screen_meta[4]-offset,consumed=0;
        if(count>wanted)count=wanted;
        unsigned encoded=rd_screen_rle_encode(screen+offset,count,bytes,out+16,496,&consumed);
        if(!encoded || !consumed){*status=3;return 0;}
        put32(out,token);put32(out+4,offset);put32(out+8,screen_meta[4]);put32(out+12,consumed);
        return 16u+encoded;
    }
    case 14: {
        if(n)break;uint32_t v[64];debug_usb_diagnostics(&usb,v);v[1]=64;
        v[56]=rd_control.state;v[57]=rd_control.stop_request;v[58]=rd_control.last_error;
        v[59]=completed_stops;v[60]=last_stop_error;v[61]=last_stop_tick;
        v[62]=polls;v[63]=last_poll;return words(out,v,64);
    }
    case 26: {
        if(n)break;uint32_t v[16];exec_info(v);return words(out,v,16);
    }
    case 27:
        if(n!=20)break;*status=(unsigned)exec_begin(get32(p),get32(p+4),get32(p+8),get32(p+12),get32(p+16));return 0;
    case 28:
        if(n<5)break;*status=(unsigned)exec_write(get32(p),p+4,n-4);return 0;
    case 29:
        if(n)break;*status=(unsigned)exec_commit();return 0;
    case 30:
        if(n<8 || n>264)break;*status=(unsigned)exec_schedule(get32(p),get32(p+4),p+8,n-8);return 0;
    case 31:
        if(n)break;return words(out,(const uint32_t *)&exec_diag,15);
    case 32:
        if(n)break;exec_request_cancel(EXEC_USER_CANCEL);return 0;
    case 33: {
        if(n!=6)break;uint32_t offset=get32(p),wanted=p[4]|((unsigned)p[5]<<8);
        if(exec_diag.state!=EXEC_DONE){*status=2;return 0;}
        if(!wanted || wanted>504 || offset>exec_diag.output_size)break;
        unsigned count=exec_read(offset,wanted,out+8);
        put32(out,offset);put32(out+4,exec_diag.output_size);return count+8;
    }
    case 34:
        if(n)break;*status=(unsigned)exec_reset();return 0;
    case 50: case 51: {
        if((op==50 && n) || (op==51 && !n))break;
        uint32_t id=0;*status=(unsigned)rd_shell_request(op==50?1u:2u,p,n,&id);
        if(*status)return 0;put32(out,id);return 4;
    }
    case 52: {
        if(n)break;uint32_t v[8];rd_shell_status(v);return words(out,v,8);
    }
    default:break;
    }
    *status=1;return 0;
}
static void release_inputs(void)
{(void)rd_keys_set(0,0,1,fw_tick());(void)rd_touch_set(0,0,0,0,fw_tick());}
static unsigned transport_poll(void)
{
    if(in_poll)return 0;in_poll=1;++polls;last_poll=fw_tick();
    unsigned result=0,old_setups=usb.setups,old_phase=usb.control_phase,old_offset=usb.control_offset;
    debug_usb_poll(&usb);
    if(usb.setups!=old_setups || usb.control_phase!=old_phase || usb.control_offset!=old_offset)result|=TRANSPORT_PROGRESS;
    unsigned cable=(unsigned)debug_usb_cable_connected();
    if(usb.resets!=reset_seen || (cable_seen && !cable) || (configured_seen && !usb.configured)){
        result|=TRANSPORT_RESTART;
        reset_seen=usb.resets;protocol.rx_count=protocol.tx_size=protocol.tx_offset=0;zlp_pending=0;
        screen_valid=0;release_inputs();exec_request_cancel(EXEC_USB_FAULT);
        rd_files_cancel(RD_FILE_TRANSPORT,fw_tick());
    }
    if(usb.session_epoch!=session_seen){
        result|=TRANSPORT_RESTART;
        protocol.rx_count=protocol.tx_size=protocol.tx_offset=0;zlp_pending=0;
        screen_valid=0;exec_request_cancel(EXEC_USB_FAULT);
        rd_files_cancel(RD_FILE_TRANSPORT,fw_tick());
        /* Closing a short-lived CLI press does not truncate its finite lease.
         * Cable loss, deconfiguration, reset and stop still release keys. */
    }
    cable_seen=cable;configured_seen=usb.configured;session_seen=usb.session_epoch;
    if(usb.ownership_fault){rd_control.last_error=0x100u|usb.fault_reason;rd_control.stop_request=1;release_inputs();exec_request_cancel(EXEC_USB_FAULT);}
    if(usb.configured && !usb.ownership_fault){
        /* Finish with a short packet, including ZLP for exact 64-byte frames.
         * Do not accept another command until the previous frame is drained. */
        if(protocol.tx_size){
            unsigned count=protocol.tx_size-protocol.tx_offset;if(count>64)count=64;
            if(count && debug_usb_write(&usb,protocol.tx+protocol.tx_offset,count)){
                result|=TRANSPORT_PROGRESS;
                protocol.tx_offset+=count;
                if(protocol.tx_offset==protocol.tx_size)zlp_pending=(protocol.tx_size%64u)==0;
            }
            if(protocol.tx_offset==protocol.tx_size){
                if(zlp_pending){if(debug_usb_write(&usb,protocol.tx,0)){zlp_pending=0;result|=TRANSPORT_PROGRESS;}}
                else if(debug_usb_tx_idle(&usb)){protocol.tx_size=protocol.tx_offset=0;result|=TRANSPORT_PROGRESS;}
            }
        }else if(!rd_control.stop_request){
            uint8_t packet[64];int count=debug_usb_read(&usb,packet,sizeof packet);
            if(count>0){result|=TRANSPORT_PROGRESS;debug_protocol_feed(&protocol,packet,(unsigned)count,dispatch);}
            else if(count<0){rd_control.last_error=0x200;rd_control.stop_request=1;}
        }
    }
    if(debug_usb_clock_recover(&usb,!exec_active() && !rd_control.stop_request &&
                               !rd_files_busy() && !rd_shell_stop_blocked()))result|=TRANSPORT_RESTART;
    rd_keys_poll(fw_tick());rd_touch_poll(fw_tick());in_poll=0;return result;
}
static void __attribute__((noinline)) transport_service(void)
{
    /* FRAME is read-only (JZ4740 PM 21.4.3.9). Full-speed SOF gives a 1 ms
     * clock without changing the verified IRQ24/PD6/TCU ownership. Only real
     * progress opens a short polling window for delayed ACKs/next requests.
     * Its deadline never extends with traffic. Individual RPC work (e.g. a
     * snapshot copy) can itself exceed this budget; it is not preempted here. */
    ++service_calls;
    uint32_t frame=debug_usb_frame(),tick=fw_tick();
    unsigned state=transport_poll(),count=1;
    if(!(state&TRANSPORT_PROGRESS)){++idle_returns;return;}
    if((state&TRANSPORT_RESTART) || rd_control.stop_request)return;
    ++active_windows;
    for(;;){
        if(rd_control.stop_request)break;
        if(((debug_usb_frame()-frame)&0x7ffu)>=2u){++frame_limit_exits;break;}
        /* SOF stops on suspend/disconnect. The native tick and fixed poll
         * cap are independent exits even if one or both clocks freeze. */
        if(fw_tick()!=tick){++tick_limit_exits;break;}
        if(count>=TRANSPORT_MAX_POLLS){++iteration_limit_exits;break;}
        state=transport_poll();++count;
        if(state&TRANSPORT_RESTART)break;
    }
    if(count>window_polls_max)window_polls_max=count;
}
static int module_poll(void)
{
    transport_service();
    if(rd_control.stop_request)exec_request_cancel(EXEC_USER_CANCEL);
    if(fw_tick()-exec_diag.start_tick>=exec_diag.budget_ticks)exec_request_cancel(EXEC_TIMEOUT);
    fw_delay(1);return (int)exec_diag.flags;
}
static unsigned module_command(unsigned op,const uint8_t *p,unsigned n,uint8_t *out,unsigned *status)
{(void)op;(void)p;(void)n;(void)out;*status=1;return 0;}
static uint32_t module_table(unsigned slot)
{
    if(slot<0x83c00004u || slot>0x83c00014u || (slot&3u))return 0;
    return rd_control.runtime_tables[(slot-0x83c00004u)/4u];
}
static uint32_t module_entry(uint32_t table,unsigned offset)
{return ram_range(table,4) && offset<0x1000u && !(offset&3u) && ram_range(table+offset,4)?*(uint32_t *)(uintptr_t)(table+offset):0;}
static const h1_exec_api module_api={1,sizeof(h1_exec_api),fw_tick,module_poll,module_command,module_table,module_entry};
static int resident_init(rd_header *h)
{
    if(h!=&rd_control || h->magic!=RD_MAGIC || !fw_matches())return 1;
    if(debug_usb_cable_connected())return 2;
    protocol=(debug_protocol){0};screen_valid=0;zlp_pending=0;in_poll=0;
    reset_seen=cable_seen=configured_seen=session_seen=0;stop_tick=0;
    h->last_error=0;h->stop_request=0;(void)exec_reset();
    int result=rd_files_init();if(result){h->last_error=0x500u|(unsigned)result;return 5;}
    result=debug_usb_open(&usb);if(result)return result;
    result=rd_io_init();if(result){debug_usb_close(&usb);return result;}
    result=rd_shell_init();if(result){h->last_error=0x600u|(unsigned)result;return 6;}
    result=fw_wake_install(h);
    if(result){h->last_error=0x300u|(unsigned)result;return 3;}
    return 0;
}
static void resident_shutdown(rd_header *h)
{
    if(exec_active()){exec_request_cancel(EXEC_USER_CANCEL);return;}
    if(rd_files_busy()){
        rd_files_cancel(RD_FILE_CANCELLED,fw_tick());rd_files_poll(fw_tick());return;
    }
    int result=rd_shell_shutdown();
    if(result){h->last_error=0x600u|(unsigned)result;return;}
    release_inputs();
    result=rd_io_shutdown();
    if(result){h->last_error=(unsigned)result;return;}
    debug_usb_close(&usb);
    result=fw_wake_remove();
    if(result){h->last_error=0x400u|(unsigned)result;return;}
    screen_valid=0;++completed_stops;last_stop_error=h->last_error;last_stop_tick=fw_tick();h->state=RD_STOPPED;
}
static void resident_worker(void *arg)
{
    rd_header *h=arg;
    for(;;){
        if(h->state==RD_RUNNING){
            if(!h->stop_request)transport_service();
            rd_shell_poll(fw_tick());
            if(h->stop_request)rd_files_cancel(RD_FILE_CANCELLED,fw_tick());
            rd_files_poll(fw_tick());
            if(exec_diag.state==EXEC_PENDING && !protocol.tx_size && debug_usb_tx_idle(&usb))exec_invoke(&module_api);
            if(h->stop_request){
                if(!stop_tick)stop_tick=fw_tick();
                transport_poll();
                if(!rd_files_busy() && ((!protocol.tx_size && debug_usb_tx_idle(&usb)) || fw_tick()-stop_tick>=40u))h->state=RD_STOPPING;
            }
        }
        if(h->state==RD_STOPPING)resident_shutdown(h);
        fw_delay(h->state==RD_STOPPED?20u:1u);
    }
}
