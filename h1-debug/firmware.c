#include "firmware.h"

#define R8(a) (*(volatile uint8_t *)(uintptr_t)(a))
#define R32(a) (*(volatile uint32_t *)(uintptr_t)(a))

static rd_header *wake_owner;
static volatile uint32_t wake_pending,wake_at;
static uint32_t wake_installed;

static uint32_t irq_lock(void)
{
    uint32_t saved,masked;
    __asm__ volatile("mfc0 %0,$12\n\tnop":"=r"(saved)::"memory");masked=saved&~1u;
    __asm__ volatile("mtc0 %0,$12\n\tnop\n\tnop\n\tnop"::"r"(masked):"memory");return saved;
}
static void irq_unlock(uint32_t saved)
{__asm__ volatile("mtc0 %0,$12\n\tnop\n\tnop\n\tnop"::"r"(saved):"memory");}

static uint32_t code_crc(uint32_t address,uint32_t length)
{
    const volatile uint8_t *p=(const volatile uint8_t *)(uintptr_t)address;
    uint32_t crc=~0u;
    while(length--){crc^=*p++;for(unsigned i=0;i<8;i++)crc=(crc>>1)^((crc&1u)?0xedb88320u:0u);}
    return ~crc;
}

int fw_matches(void)
{
    /* Selected original code fingerprints, not whole-firmware authentication.
     * Includes context-frame construction, TCB initialization, scheduling and
     * the untracked global allocator; no inferred SDK thread-table slots. */
    static const struct {uint32_t address,length,crc;} regions[]={
        {0x8000db5cu,0x184u,0xa42f672cu},
        {0x8000dce0u,0x240u,0x12667fffu},
        {0x8000e8fcu,0xccu,0xad152cc4u},
        {0x800099d4u,0x1a0u,0xb236597bu},
        {0x80009080u,0xf0u,0x05d5088du},
        {0x800db480u,0x7cu,0xc750fa0du},
        {0x800dc998u,0x50u,0xcdfbf6dbu},
        {0x800dbb54u,0x60u,0xa3b2c4aau},
        {0x800dc790u,0x18cu,0xa9df46d7u},
        {0x80009770u,0xd8u,0x05711fe2u},
        {0x8000e810u,0xecu,0xe85900fdu},
        {0x80165988u,0x10cu,0xdc816a91u}
    };
    for(unsigned i=0;i<sizeof regions/sizeof regions[0];i++)
        if(code_crc(regions[i].address,regions[i].length)!=regions[i].crc)return 0;
    return R32(FW_RAW_TICK)==0x3c028058u &&
           R32(FW_RAW_TICK+4)==0x03e00008u &&
           R32(FW_RAW_TICK+8)==0x8c42b064u;
}

uint32_t fw_tick(void)
{return ((uint32_t (*)(void))(uintptr_t)FW_RAW_TICK)();}

void fw_delay(uint32_t ticks)
{
    if(!ticks)return;
    uint32_t current=R32(0x8057adfcu);
    if(!current || R8(current+0x35u)!=RD_PRIORITY){
        /* Stock V1.41 leaves its OSTimeTick initialization disabled. A GUI
         * caller must keep IRQs enabled rather than sleep on its inert timer. */
        uint32_t begin=fw_tick();
        for(unsigned n=0;n<20000000u && fw_tick()-begin<ticks;n++){}
        return;
    }
    /* Publish deadline and block atomically: otherwise an IRQ could consume
     * the wakeup immediately before OSTimeDly removes this task from ready. */
    uint32_t saved=irq_lock();wake_at=fw_tick()+ticks;wake_pending=1;
    ((void (*)(uint32_t))(uintptr_t)FW_TASK_DELAY)(1u);
    irq_unlock(saved);
}

static void tick_irq(void *argument)
{
    /* Preserve the complete original tick/key housekeeping. Wake only our
     * task, without enabling the dormant system-wide RTOS timer. ISR exit
     * performs the normal scheduler switch after this callback returns. */
    ((void (*)(void *))(uintptr_t)0x80165988u)(argument);
    if(wake_pending && (int32_t)(fw_tick()-wake_at)>=0){
        uint32_t tcb=R32(FW_TASK_TABLE+4u*RD_PRIORITY);
        if(tcb>=0x80004000u && tcb<0x83bfff90u && !(tcb&3u) &&
           R32(tcb+4u)==(uint32_t)(uintptr_t)wake_owner && R32(tcb+0x30u)){
            unsigned result=((unsigned (*)(unsigned))(uintptr_t)0x8000e810u)(RD_PRIORITY);
            if(!result)wake_pending=0;
        }
    }
}

int fw_wake_install(rd_header *h)
{
    uint32_t saved=irq_lock();int result=0;
    if(wake_installed || R32(FW_TICK_IRQ_SLOT)!=0x80165988u || R32(FW_TICK_IRQ_SLOT+4u))result=1;
    else {wake_owner=h;wake_installed=1;R32(FW_TICK_IRQ_SLOT)=(uint32_t)(uintptr_t)tick_irq;}
    irq_unlock(saved);return result;
}

int fw_wake_remove(void)
{
    uint32_t saved=irq_lock();int result=0;
    if(wake_installed){
        if(R32(FW_TICK_IRQ_SLOT)!=(uint32_t)(uintptr_t)tick_irq || R32(FW_TICK_IRQ_SLOT+4u))result=1;
        else {R32(FW_TICK_IRQ_SLOT)=0x80165988u;wake_installed=0;}
    }
    irq_unlock(saved);return result;
}

void *fw_alloc(uint32_t bytes)
{return ((void *(*)(uint32_t))(uintptr_t)0x800dc998u)(bytes);}
void fw_free(void *pointer)
{((void (*)(void *))(uintptr_t)0x800dbb54u)(pointer);}

static int ram_range(uint32_t address,uint32_t bytes)
{return address>=0x80004000u && address<0x83c00000u && bytes<=0x83c00000u-address;}
static int resident_pointer(const rd_header *h,uint32_t address)
{return !(address&3u) && address>=h->base && address-h->base<h->size;}
static int valid_control(rd_header *h)
{
    uint32_t address=(uint32_t)(uintptr_t)h;
    if((address&31u) || !ram_range(address,sizeof *h))return 0;
    if(h->magic!=RD_MAGIC || h->version!=RD_VERSION || h->base!=address ||
       h->priority!=RD_PRIORITY || h->size<sizeof *h || h->size>0x400000u ||
       !ram_range(address,h->size) || !ram_range(h->allocation,h->size) ||
       h->allocation>address || address-h->allocation>31u || h->state>RD_FAULT)return 0;
    if(!resident_pointer(h,(uint32_t)(uintptr_t)h->init) ||
       !resident_pointer(h,(uint32_t)(uintptr_t)h->worker) ||
       !resident_pointer(h,(uint32_t)(uintptr_t)h->shutdown))return 0;
    return resident_pointer(h,h->stack_bottom) && resident_pointer(h,h->stack_top) &&
           h->stack_top>=h->stack_bottom && h->stack_top-h->stack_bottom==RD_STACK_BYTES-4u;
}

int fw_task_slot_empty(void)
{return R32(FW_TASK_TABLE+4u*RD_PRIORITY)==0;}

rd_header *fw_task_control(void)
{
    /* OSTaskCreateExt keeps the supplied ext pointer in TCB+4. No fixed RAM
     * mailbox or overwritten BDA service slot is used for rediscovery. */
    uint32_t tcb=R32(FW_TASK_TABLE+4u*RD_PRIORITY);
    if((tcb&3u) || !ram_range(tcb,0x70u) || R8(tcb+0x35u)!=RD_PRIORITY)return 0;
    rd_header *h=(rd_header *)(uintptr_t)R32(tcb+4u);
    if(!valid_control(h))return 0;
    if(R32(tcb+8u)!=h->stack_bottom || R32(tcb+12u)!=RD_STACK_BYTES/4u)return 0;
    return h;
}

int fw_task_create(rd_header *h)
{
    typedef unsigned (*create_fn)(void (*)(void *),void *,uint32_t *,unsigned,
                                  unsigned,uint32_t *,uint32_t,void *,unsigned);
    if(!valid_control(h) || !fw_task_slot_empty())return -1;
    /* Running RTOS, task context, no scheduler lock. Slot6 is above the busy
     * GUI task7; the worker must yield with fw_delay after bounded service. */
    if(R8(0x8057ad91u)!=1 || R8(0x8057adf9u) || R8(0x8057ad90u))return -2;
    return (int)((create_fn)(uintptr_t)FW_TASK_CREATE_EXT)(h->worker,h,
        (uint32_t *)(uintptr_t)h->stack_top,RD_PRIORITY,0x4442u,
        (uint32_t *)(uintptr_t)h->stack_bottom,RD_STACK_BYTES/4u,h,0u);
}

int fw_task_delete(uint32_t priority)
{
    rd_header *h=fw_task_control();
    if(priority!=RD_PRIORITY || !h || h->state!=RD_STOPPED)return -1;
    /* Normal stop keeps this block and dormant task: the stock GUI event
     * queue or a consumer may still hold pointers into immutable key records.
     * Reserved for controlled validation with no published event pointers. */
    return (int)((unsigned (*)(unsigned))(uintptr_t)FW_TASK_DELETE)(priority);
}
