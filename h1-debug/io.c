/* Firmware evidence: build/minigpt-native/project.bin, SHA256
 * d05786e442f9aad62a8d0a0cb4f6d786bdc7c2fa353a7a2b152c9ed9f01b40ef.
 * Key queue: native scanner 800085C4/80008760 posts [matrix,event] to
 * *(void **)8057AE10 with OSQPost 8000CDDC. That queue stores POINTERS.
 * Held query: GUI+9D8 -> 80068AE8 -> 80008234. Its first two instructions
 * are relocation-free and copied into a resident trampoline.
 * LCD descriptor setup: 80005B48, width/height/bpp at 803F2404/08/0C;
 * descriptor words next/source/id/command, physical RAM address fields.
 */
#include "io.h"
#include "debug.h"

#define RD_READ32(a) (*(volatile uint32_t *)(uintptr_t)(a))
#define RD_READ8(a) (*(volatile uint8_t *)(uintptr_t)(a))
#define RD_QUERY 0x80008234u
#define RD_QUERY_0 0x3c02802au
#define RD_QUERY_1 0x8c424aa4u
#define RD_QUEUE_SLOT 0x8057ae10u
#define RD_QUEUE_POST 0x8000cddcu
#define RD_RAM_SIZE 0x04000000u
#define RD_LCD 0xb3050000u
#define RD_SCREEN_BYTES (480u*272u*4u)

static volatile uint32_t held[2];
static uint32_t emitted[2],lease_started,lease_length;
static uint32_t key_records[42][2][2];
static uint32_t query_trampoline[4] __attribute__((aligned(16)));
static uint32_t installed_jump;
static unsigned records_initialized,hook_installed;
static const uint8_t native_codes[42]={16,17,18,19,20,21,22,30,31,32,33,34,35,36,
    104,44,45,46,47,48,49,109,57,1,28,105,108,106,23,24,25,37,38,86,
    106,50,103,111,28,105,1,42};

static uint32_t io_lock(void)
{
    uint32_t old,disabled;
    __asm__ volatile("mfc0 %0,$12\n\tnop":"=r"(old));
    disabled=old&~1u;
    __asm__ volatile("mtc0 %0,$12\n\tnop\n\tnop\n\tnop"::"r"(disabled):"memory");
    return old;
}
static void io_unlock(uint32_t old)
{
    __asm__ volatile("mtc0 %0,$12\n\tnop\n\tnop\n\tnop"::"r"(old):"memory");
}
static int ram_pointer(uint32_t pointer,uint32_t bytes)
{
    uint32_t segment=pointer&0xe0000000u,physical=pointer&0x1fffffffu;
    return (segment==0x80000000u || segment==0xa0000000u) &&
        physical<RD_RAM_SIZE && bytes<=RD_RAM_SIZE-physical;
}
static int physical_pointer(uint32_t pointer,uint32_t bytes)
{
    return pointer<RD_RAM_SIZE && bytes<=RD_RAM_SIZE-pointer;
}
static int key_query(unsigned code)
{
    typedef int (*query_fn)(unsigned);
    /* Preserve the hardware scan and firmware housekeeping even when a
     * synthetic key is down. Aliases such as Enter/Confirm are ORed. */
    int physical=((query_fn)(uintptr_t)query_trampoline)(code);
    code&=255u;
    for(unsigned i=0;i<42;++i)
        if(native_codes[i]==code && (held[i>>5]&(1u<<(i&31u))))return 1;
    return physical;
}
static int io_firmware_matches(void)
{
    return RD_READ32(RD_QUEUE_POST)==0x27bdffe0u &&
        RD_READ32(RD_QUEUE_POST+4)==0xafb00010u &&
        RD_READ32(0x80005b48u)==0x3c02803fu &&
        RD_READ32(0x80005b4cu)==0x8c422408u &&
        RD_READ32(0x80005824u)==0x3c02803fu &&
        RD_READ32(0x80005828u)==0x03e00008u &&
        RD_READ32(0x8000582cu)==0x8c422438u;
}
static int own_hook(void)
{
    return RD_READ32(RD_QUERY)==installed_jump && RD_READ32(RD_QUERY+4)==0;
}
/* V1.41 SADC IRQ 80041CC8 posts [0,11/12/8] to the input queue. The
 * coordinate getter 80040EBC supplies calibrated pixels to native GUI
 * translation 8011497C and exported GUI+6C0. Bind coordinates at dequeue,
 * rather than racing a queued DOWN against a newer MOVE's global position.
 * OSQ* consumers receive permanent immutable proxies, never a reusable slot.
 * We deliberately do not change physical ADC samples or its state byte. */
#define TOUCH_SLOTS 64u
typedef struct {uint32_t address,first,second,trampoline[10],jump;} touch_hook;
static touch_hook touch_hooks[4]={
    {0x80040ed4u,0xafb50044u,0xafb40040u,{0},0},
    {0x8000c8b0u,0x27bdffe0u,0xafb10014u,{0},0},
    {0x8000cc5cu,0x3c028058u,0x9042adf9u,{0},0},
    {0x8000d208u,0x27bdffe8u,0xafb00010u,{0},0}
};
static uint32_t touch_points[TOUCH_SLOTS][2],touch_used[TOUCH_SLOTS];
static const uint32_t touch_events[3][2]={{0,11},{0,12},{0,8}};
static uint32_t touch_x,touch_y,touch_pressed,touch_started,touch_lease;
static uint32_t touch_view_x,touch_view_y,touch_view_event,touch_view_owner;
static uint32_t touch_view_valid,touch_view_read,touch_pending_up,touch_outstanding,touch_installed;
static int event_queue(uint32_t *queue,uint32_t *free_slots);
static const uint32_t touch_prefix[6]={0x27bdffa8u,0xafbf0050u,0xafb7004cu,
    0xafb60048u,0x00a0b821u,0x0080b021u};
/* Existing BDA wrappers validate coordinate-getter words 0/1/4/5. Keep its
 * first six instructions unchanged. The patched +24 entry reaches this
 * bridge after saving RA/S7/S6 and assigning S7/S6; unwind just that prefix
 * before entering an ordinary o32 C function. A0/A1 are still intact. */
extern void rd_touch_coordinate_bridge(uint16_t *,uint16_t *);
__asm__(".section .text.rd_touch_coordinate_bridge,\"ax\",@progbits\n"
        ".set push\n.set noreorder\n.globl rd_touch_coordinate_bridge\n"
        "rd_touch_coordinate_bridge:\n"
        "lw $s6,0x48($sp)\n lw $s7,0x4c($sp)\n lw $ra,0x50($sp)\n"
        "j touch_coordinates\n addiu $sp,$sp,0x58\n"
        ".set pop\n.previous\n");
static int touch_prefix_matches(void)
{
    for(unsigned i=0;i<6;++i)if(RD_READ32(0x80040ebcu+4u*i)!=touch_prefix[i])return 0;
    return 1;
}

static int touch_owned(void)
{
    if(!touch_installed || !touch_prefix_matches())return 0;
    for(unsigned i=0;i<4;++i)
        if(RD_READ32(touch_hooks[i].address)!=touch_hooks[i].jump ||
           RD_READ32(touch_hooks[i].address+4))return 0;
    return 1;
}
static int touch_slot(uint32_t pointer)
{
    uint32_t begin=(uint32_t)(uintptr_t)touch_points;
    if(pointer<begin || pointer-begin>=sizeof touch_points || ((pointer-begin)&7u))return -1;
    return (int)((pointer-begin)/8u);
}
/* Next input dequeue by the same task proves it finished using the prior
 * returned event's coordinates. In particular, an UP must survive until here
 * before shutdown may remove the coordinate hook. Other queues do not count. */
static void touch_before_read(void *queue)
{
    if((uint32_t)(uintptr_t)queue!=RD_READ32(RD_QUEUE_SLOT))return;
    uint32_t irq=io_lock();
    if(touch_view_valid && touch_view_event==8u &&
       touch_view_owner==RD_READ32(0x8057adfcu))touch_view_valid=0;
    io_unlock(irq);
}
static void *touch_after_read(void *queue,void *message)
{
    if(!message || (uint32_t)(uintptr_t)queue!=RD_READ32(RD_QUEUE_SLOT))return message;
    uint32_t irq=io_lock();int slot=touch_slot((uint32_t)(uintptr_t)message);
    if(slot>=0 && touch_used[slot]){
        uint32_t packed=touch_points[slot][0],event=touch_points[slot][1];
        touch_view_x=packed&0xffffu;touch_view_y=packed>>16;
        touch_view_event=event;touch_view_owner=RD_READ32(0x8057adfcu);
        touch_view_valid=1;touch_view_read=0;touch_used[slot]=0;--touch_outstanding;
        message=(void *)(uintptr_t)touch_events[event==11u?0:event==12u?1:2];
    }else {
        /* A physical pen event resumes native coordinate handling. */
        uint32_t pointer=(uint32_t)(uintptr_t)message;
        if(ram_pointer(pointer,8)){
            uint32_t event=RD_READ32(pointer+4);
            if(event==8u || event==11u || event==12u)touch_view_valid=0;
        }
    }
    io_unlock(irq);return message;
}
static void __attribute__((used,noinline)) touch_coordinates(uint16_t *x,uint16_t *y)
{
    uint32_t irq=io_lock(),valid=touch_view_valid;
    if(valid){
        *x=(uint16_t)touch_view_x;*y=(uint16_t)touch_view_y;
        /* Native GUI 8011497C copies these outputs into its own event before
         * launching a BDA. That BDA may stop us without another input read.
         * Only the task consuming this UP can acknowledge its coordinates. */
        if(touch_view_event==8u && touch_view_owner==RD_READ32(0x8057adfcu))touch_view_read=1;
    }
    io_unlock(irq);
    if(!valid)((void (*)(uint16_t *,uint16_t *))(uintptr_t)touch_hooks[0].trampoline)(x,y);
}
static void *touch_accept(void *queue,uint8_t *error)
{
    touch_before_read(queue);
    void *result=((void *(*)(void *,uint8_t *))(uintptr_t)touch_hooks[1].trampoline)(queue,error);
    return touch_after_read(queue,result);
}
static void *touch_pend(void *queue,uint32_t timeout,uint8_t *error)
{
    touch_before_read(queue);
    void *result=((void *(*)(void *,uint32_t,uint8_t *))(uintptr_t)touch_hooks[2].trampoline)(queue,timeout,error);
    return touch_after_read(queue,result);
}
static unsigned touch_flush(void *queue)
{
    uint32_t irq=io_lock(),q,available;
    if((uint32_t)(uintptr_t)queue==RD_READ32(RD_QUEUE_SLOT) && !event_queue(&q,&available)){
        uint32_t ring=RD_READ32(q+8),start=RD_READ32(ring+4),end=RD_READ32(ring+8);
        uint32_t read=RD_READ32(ring+16),used=50u-available;
        if(ram_pointer(start,200) && end==start+200u && read>=start && read<end && !(read&3u)){
            for(unsigned n=0;n<used;++n){
                int slot=touch_slot(RD_READ32(read));
                if(slot>=0 && touch_used[slot]){touch_used[slot]=0;--touch_outstanding;}
                read+=4u;if(read==end)read=start;
            }
            /* A native flush can discard our UP as well as MOVE. Always
             * reconcile a virtual contact that was already delivered. */
            if(touch_pressed || (touch_view_valid && touch_view_event!=8u))touch_pending_up=1;
            touch_pressed=0;touch_lease=0;
        }
    }
    unsigned result=((unsigned (*)(void *))(uintptr_t)touch_hooks[3].trampoline)(queue);
    io_unlock(irq);return result;
}
static int touch_prepare(void)
{
    uint32_t entries[4]={(uint32_t)(uintptr_t)rd_touch_coordinate_bridge,(uint32_t)(uintptr_t)touch_accept,
        (uint32_t)(uintptr_t)touch_pend,(uint32_t)(uintptr_t)touch_flush};
    if(!touch_prefix_matches())return 1;
    for(unsigned i=0;i<4;++i){
        touch_hook *h=&touch_hooks[i];
        if(RD_READ32(h->address)!=h->first || RD_READ32(h->address+4)!=h->second)return 1;
        if((entries[i]&0xf0000000u)!=(h->address&0xf0000000u))return 1;
        unsigned prefix=i?0u:6u;
        for(unsigned j=0;j<prefix;++j)h->trampoline[j]=touch_prefix[j];
        h->trampoline[prefix]=h->first;h->trampoline[prefix+1]=h->second;
        h->trampoline[prefix+2]=0x08000000u|((h->address+8u)>>2&0x03ffffffu);
        h->trampoline[prefix+3]=0;
        h->jump=0x08000000u|(entries[i]>>2&0x03ffffffu);
        rd_cache_sync(h->trampoline,h->trampoline+prefix+4);
    }
    return 0;
}
static int touch_install(void)
{
    if(!touch_prefix_matches())return 3;
    for(unsigned i=0;i<4;++i)
        if(RD_READ32(touch_hooks[i].address)!=touch_hooks[i].first ||
           RD_READ32(touch_hooks[i].address+4)!=touch_hooks[i].second)return 3;
    for(unsigned i=0;i<4;++i){touch_hook *h=&touch_hooks[i];
        RD_READ32(h->address)=h->jump;RD_READ32(h->address+4)=0;
        rd_cache_sync((void *)(uintptr_t)h->address,(void *)(uintptr_t)(h->address+8));
    }
    touch_installed=1;return 0;
}
static int touch_post(uint32_t event)
{
    uint32_t queue,available;
    if(event_queue(&queue,&available))return 3;
    if(!available)return 2;
    unsigned slot=0;while(slot<TOUCH_SLOTS && touch_used[slot])++slot;
    if(slot==TOUCH_SLOTS)return 2;
    touch_points[slot][0]=touch_x|(touch_y<<16);touch_points[slot][1]=event;
    touch_used[slot]=1;++touch_outstanding;
    unsigned result=((unsigned (*)(void *,void *))(uintptr_t)RD_QUEUE_POST)
        ((void *)(uintptr_t)queue,touch_points[slot]);
    if(result){touch_used[slot]=0;--touch_outstanding;return result==30u?2:3;}
    return 0;
}
static int touch_release(void)
{
    if(touch_pressed)touch_pending_up=1;
    touch_pressed=0;touch_lease=0;
    if(!touch_pending_up)return 0;
    int result=touch_post(8u);if(!result)touch_pending_up=0;return result;
}
int rd_touch_set(uint32_t x,uint32_t y,uint32_t pressed,uint32_t ticks,uint32_t now)
{
    if(pressed>1u || (pressed && (x>=480u || y>=272u || !ticks || ticks>4000u)))return 1;
    if(!touch_owned())return 3;
    uint32_t irq=io_lock();
    if(!pressed){int result=touch_release();io_unlock(irq);return result;}
    if(touch_pending_up){io_unlock(irq);return 2;}
    uint32_t old_x=touch_x,old_y=touch_y,event=touch_pressed?12u:11u;
    int changed=!touch_pressed || x!=touch_x || y!=touch_y;
    touch_x=x;touch_y=y;int result=changed?touch_post(event):0;
    if(result){touch_x=old_x;touch_y=old_y;io_unlock(irq);return result;}
    touch_pressed=1;touch_started=now;touch_lease=ticks;io_unlock(irq);return 0;
}
void rd_touch_poll(uint32_t now)
{
    uint32_t irq=io_lock();
    if(touch_pending_up || (touch_lease && now-touch_started>=touch_lease))(void)touch_release();
    io_unlock(irq);
}
void rd_touch_get(uint32_t status[4],uint32_t now)
{
    uint32_t irq=io_lock(),elapsed=now-touch_started;
    status[0]=touch_x;status[1]=touch_y;status[2]=touch_pressed;
    status[3]=touch_pressed && elapsed<touch_lease?touch_lease-elapsed:0;
    io_unlock(irq);
}
static int touch_shutdown(void)
{
    if(!touch_installed)return 0;
    if(!touch_owned())return 3;
    int result=touch_release();if(result)return result;
    if(touch_outstanding || (touch_view_valid && (touch_view_event!=8u || !touch_view_read)))return 2;
    for(unsigned i=0;i<4;++i){touch_hook *h=&touch_hooks[i];
        RD_READ32(h->address)=h->first;RD_READ32(h->address+4)=h->second;
        rd_cache_sync((void *)(uintptr_t)h->address,(void *)(uintptr_t)(h->address+8));
    }
    touch_view_valid=0;touch_installed=0;return 0;
}
int rd_io_init(void)
{
    if(!io_firmware_matches())return 1;
    if(hook_installed)return own_hook() && touch_owned()?0:3;
    if(RD_READ32(RD_QUERY)!=RD_QUERY_0 || RD_READ32(RD_QUERY+4)!=RD_QUERY_1)return 1;
    if(touch_prepare())return 1;
    if(!records_initialized){
        for(unsigned i=0;i<42;++i)for(unsigned down=0;down<2;++down){
            key_records[i][down][0]=i+1;
            key_records[i][down][1]=down?9u:10u;
        }
        records_initialized=1;
    }
    uint32_t handler=(uint32_t)(uintptr_t)key_query;
    if((handler&0xf0000000u)!=(RD_QUERY&0xf0000000u))return 1;
    query_trampoline[0]=RD_QUERY_0;query_trampoline[1]=RD_QUERY_1;
    query_trampoline[2]=0x08000000u|((RD_QUERY+8u)>>2&0x03ffffffu);
    query_trampoline[3]=0;
    rd_cache_sync(query_trampoline,query_trampoline+4);
    installed_jump=0x08000000u|(handler>>2&0x03ffffffu);
    uint32_t irq=io_lock();
    if(RD_READ32(RD_QUERY)!=RD_QUERY_0 || RD_READ32(RD_QUERY+4)!=RD_QUERY_1){io_unlock(irq);return 3;}
    if(touch_install()){io_unlock(irq);return 3;}
    held[0]=held[1]=0;lease_length=0;
    RD_READ32(RD_QUERY)=installed_jump;RD_READ32(RD_QUERY+4)=0;
    rd_cache_sync((void *)(uintptr_t)RD_QUERY,(void *)(uintptr_t)(RD_QUERY+8));
    hook_installed=1;io_unlock(irq);return 0;
}
/* Caller holds IRQ lock. Validate the native queue before inspecting its
 * free slots; this never drains, rewrites, or flushes existing user events. */
static int event_queue(uint32_t *queue,uint32_t *free_slots)
{
    uint32_t q=RD_READ32(RD_QUEUE_SLOT);
    if((q&3u) || !ram_pointer(q,12) || RD_READ8(q)!=2)return 1;
    uint32_t ring=RD_READ32(q+8);
    if((ring&3u) || !ram_pointer(ring,28))return 1;
    uint32_t capacity=RD_READ32(ring+20),used=RD_READ32(ring+24);
    /* The keyboard queue is created with fifty slots by 8000AF40. */
    if(capacity!=50 || used>capacity)return 1;
    *queue=q;*free_slots=capacity-used;return 0;
}
static unsigned changed_count(uint32_t low,uint32_t high)
{
    unsigned count=0;uint32_t bits[2]={emitted[0]^low,emitted[1]^high};
    for(unsigned j=0;j<2;++j)while(bits[j]){bits[j]&=bits[j]-1;++count;}
    return count;
}
/* Reconcile queued state after each accepted transition. Unexpected queue
 * failure is observable and the next call resumes at the exact remainder. */
static int emit_keys(uint32_t low,uint32_t high,int require_all)
{
    typedef unsigned (*post_fn)(void *,void *);
    uint32_t desired[2]={low,high},queue,available;
    if(!changed_count(low,high))return 0;
    if(event_queue(&queue,&available))return 3;
    if(require_all && changed_count(low,high)>available)return 2;
    for(unsigned i=0;i<42;++i){
        unsigned word=i>>5;uint32_t bit=1u<<(i&31u);
        if(!((emitted[word]^desired[word])&bit))continue;
        if(!available)return 2;
        unsigned down=!!(desired[word]&bit);
        unsigned result=((post_fn)(uintptr_t)RD_QUEUE_POST)((void *)(uintptr_t)queue,key_records[i][down]);
        if(result)return result==30u?2:3;
        --available;
        if(down)emitted[word]|=bit;else emitted[word]&=~bit;
        /* A partially accepted call must expose precisely its accepted
         * state, except lease expiry/shutdown which force held state zero. */
        if(require_all){if(down)held[word]|=bit;else held[word]&=~bit;}
    }
    return 0;
}
int rd_keys_set(uint32_t low,uint32_t high,uint32_t ticks,uint32_t now)
{
    if(high&~0x3ffu || ((low||high) && (!ticks || ticks>4000u)))return 1;
    if(!hook_installed || !own_hook())return 3;
    uint32_t irq=io_lock();
    if(!low && !high){
        held[0]=held[1]=0;lease_length=0;
        int released=emit_keys(0,0,0);io_unlock(irq);return released;
    }
    uint32_t previous_low=emitted[0],previous_high=emitted[1];
    int result=emit_keys(low,high,1);
    if(result && emitted[0]==previous_low && emitted[1]==previous_high){io_unlock(irq);return result;}
    if(!result){held[0]=low;held[1]=high;}
    /* A partially queued request still gets a lease, so even unexpected
     * firmware queue failure cannot leave newly accepted keys permanent. */
    if(held[0]||held[1]){lease_started=now;lease_length=ticks?ticks:1u;}
    else lease_length=0;
    io_unlock(irq);return result;
}
void rd_keys_poll(uint32_t now)
{
    uint32_t irq=io_lock();
    if(lease_length && now-lease_started>=lease_length){held[0]=held[1]=0;lease_length=0;}
    if(!held[0] && !held[1] && (emitted[0]||emitted[1]))(void)emit_keys(0,0,0);
    io_unlock(irq);
}
void rd_keys_get(uint32_t *low,uint32_t *high)
{
    uint32_t irq=io_lock();*low=held[0];*high=held[1];io_unlock(irq);
}
int rd_io_shutdown(void)
{
    uint32_t irq=io_lock();held[0]=held[1]=0;lease_length=0;
    int result=emit_keys(0,0,0);
    if(result){io_unlock(irq);return result;}
    if(hook_installed && !own_hook()){io_unlock(irq);return 3;}
    result=touch_shutdown();if(result){io_unlock(irq);return result;}
    if(hook_installed){
        if(!own_hook()){io_unlock(irq);return 3;}
        RD_READ32(RD_QUERY)=RD_QUERY_0;RD_READ32(RD_QUERY+4)=RD_QUERY_1;
        rd_cache_sync((void *)(uintptr_t)RD_QUERY,(void *)(uintptr_t)(RD_QUERY+8));hook_installed=0;
    }
    io_unlock(irq);return 0;
}
int rd_screen_capture(uint8_t *dest,uint32_t capacity,uint32_t metadata[6])
{
    if(!dest || !metadata || capacity<RD_SCREEN_BYTES || !io_firmware_matches())return 1;
    uint32_t width=RD_READ32(0x803f2404u),height=RD_READ32(0x803f2408u);
    uint32_t bits=RD_READ32(0x803f240cu),control=RD_READ32(RD_LCD+0x30);
    if(width!=480 || height!=272 || !(control&8u))return 2;
    unsigned bytes=bits==16?2u:(bits==18 || bits==32)?4u:0u;
    if(!bytes)return 2;
    uint32_t desc=RD_READ32(RD_LCD+0x40)&0x1fffffffu;
    if((desc&15u) || !physical_pointer(desc,16))return 2;
    uint32_t uncached_desc=0xa0000000u|desc;
    uint32_t next=RD_READ32(uncached_desc),source=RD_READ32(uncached_desc+4);
    uint32_t command=RD_READ32(uncached_desc+12),total=width*height*bytes;
    /* V1.41's display descriptor points back to itself. Refuse palettes,
     * partial frames, unknown chains and source pointers outside SDRAM. */
    if((next&0x1fffffffu)!=desc || (command&0x10000000u) ||
       (command&0x00ffffffu)!=total/4u || (source&3u) ||
       !physical_pointer(source,total))return 2;
    uint32_t address=0xa0000000u|source;
    const volatile uint32_t *pixels=(const volatile uint32_t *)(uintptr_t)address;
    for(uint32_t i=0;i<total/4u;++i){uint32_t v=pixels[i];
        dest[4*i]=(uint8_t)v;dest[4*i+1]=(uint8_t)(v>>8);
        dest[4*i+2]=(uint8_t)(v>>16);dest[4*i+3]=(uint8_t)(v>>24);
    }
    /* A buffer swap during the copy invalidates the requested snapshot. */
    if((RD_READ32(RD_LCD+0x40)&0x1fffffffu)!=desc ||
       RD_READ32(uncached_desc+4)!=source || RD_READ32(uncached_desc+12)!=command)return 3;
    metadata[0]=width;metadata[1]=height;metadata[2]=width*bytes;
    metadata[3]=bytes==2?1u:2u;metadata[4]=total;metadata[5]=address;
    return 0;
}
