/* Independent PIO CDC driver. References: JZ4740 PM ch.21; Linux musb regs.
 * No DMA, flash, native code patch, disk interface or arbitrary RAM access.
 * Only IRQ24 and the native PD6 USB cable-detect IRQ are temporarily masked.
 * Other IRQs, including the scheduler, remain enabled. Start unplugged. */
#include "usb.h"
#include "firmware.h"
#include "h1_sdk.h"
#define U 0xb3040000u
#define R8(a) (*(volatile uint8_t *)(a))
#define R16(a) (*(volatile uint16_t *)(a))
#define R32(a) (*(volatile uint32_t *)(a))
#define USB_IRQ (1u<<24)
#define PD6 (1u<<6)
static uint32_t lock(void){uint32_t v,t;__asm__ volatile("mfc0 %0,$12\n\tnop":"=r"(v));t=v&~1u;__asm__ volatile("mtc0 %0,$12\n\tnop\n\tnop\n\tnop"::"r"(t):"memory");return v;}
static void unlock(uint32_t v){__asm__ volatile("mtc0 %0,$12\n\tnop\n\tnop\n\tnop"::"r"(v):"memory");}
static const uint8_t device[]={18,1,0,2,2,2,1,64,0xc4,7,0xa6,0xa4,0,1,1,2,3,1};
/* EP1 bulk in/out, EP2 notification. Fixed JZ4740 FIFOs, FS 64-byte packets. */
static const uint8_t config[]={
    9,2,67,0,2,1,0,0x80,50,
    9,4,0,0,1,2,2,1,0,
    5,0x24,0,0x10,1, 5,0x24,1,0,1, 4,0x24,2,2, 5,0x24,6,0,1,
    7,5,0x82,3,16,0,16,
    9,4,1,0,2,0x0a,0,0,0,
    7,5,1,2,64,0,0, 7,5,0x81,2,64,0,0};
static unsigned le16(const uint8_t *p){return p[0]|((unsigned)p[1]<<8);}
int debug_usb_cable_connected(void){return !!((R32(0xb0010300)&PD6) || (R32(0xb0010100)&(1u<<18)));}
unsigned debug_usb_frame(void){return R16(U+0x0c)&0x7ffu;}
static void ownership_error(debug_usb *u,unsigned events)
{
    u->fault_txe=R16(U+6);u->fault_rxe=R16(U+8);u->fault_usbe=R8(U+0xb);
    u->fault_imr=R32(0xb0001004);u->fault_pdmask=R32(0xb0010320);
    u->fault_pin=R32(0xb0010300);u->fault_power=R8(U+1);u->fault_events=(uint8_t)events;
    u->fault_reason=(u->fault_txe?1:0)|(u->fault_rxe?2:0)|(u->fault_usbe?4:0)|
        (!(u->fault_imr&USB_IRQ)?8:0)|(!(u->fault_pdmask&PD6)?16:0)|((u->fault_power&0x20)?32:0);
    u->ownership_fault=1;
}
static void stall(debug_usb *u){R16(U+0x12)=0x60;u->control_phase=0;++u->stalls;}
static void flush_in(void)
{
    for(unsigned bank=0;bank<2 && (R16(U+0x12)&1);++bank)R16(U+0x12)=0x2008;
}
static void flush_out(void)
{
    for(unsigned bank=0;bank<2 && (R16(U+0x16)&1);++bank)R16(U+0x16)=0x10;
}
static void flush_session(void)
{
    /* CDC DTR changes do not reset the host's bulk DATA0/DATA1 state.
     * Flush stale packets, but omit TXCSR.CLRDATATOG (0x40) and
     * RXCSR.CLRDATATOG (0x80). JZ4740 PM 21.4.3.15/.17:
     * TX FLUSHFIFO=0x08, RX FLUSHFIFO=0x10. Flushing without PKTRDY
     * can corrupt data; recheck it for each of the two possible banks. */
    for(unsigned ep=1;ep<=2;++ep){
        R8(U+0x0e)=(uint8_t)ep;flush_in();
    }
    R8(U+0x0e)=1;flush_out();
    R8(U+0x0e)=0;
}
static void endpoints(void)
{
    /* Bus reset / SET_CONFIGURATION reset DATA toggles on both USB peers.
     * FIFO flushes still obey PKTRDY, even when reset left them empty. */
    R8(U+0x0e)=1;R16(U+0x10)=64;R16(U+0x14)=64;
    flush_in();flush_out();R16(U+0x12)=0x2040;R16(U+0x16)=0x80;
    R8(U+0x0e)=2;R16(U+0x10)=16;flush_in();R16(U+0x12)=0x2040;
    R8(U+0x0e)=0;
}
static void status(debug_usb *u){R16(U+0x12)=0x48;u->control_phase=0;}
static void send_control(debug_usb *u)
{
    unsigned n=u->control_length-u->control_offset;if(n>64)n=64;
    for(unsigned i=0;i<n;++i)R8(U+0x20)=u->control[u->control_offset+i];
    u->control_offset+=(uint16_t)n;
    int end=u->control_offset==u->control_length;
    R16(U+0x12)=(uint16_t)(2|(end?8:0));if(end)u->control_phase=0;
}
static void descriptor(debug_usb *u,unsigned type,unsigned index,unsigned requested)
{
    const uint8_t *p=0;unsigned n=0;
    if(type==1 && index==0){p=device;n=sizeof device;}
    if(type==2 && index==0){p=config;n=sizeof config;}
    if(type==3){
        if(index==0){u->control[0]=4;u->control[1]=3;u->control[2]=9;u->control[3]=4;n=4;}
        else {
            const char *s=index==1?"BBK H1 lab":index==2?"H1 USB Debug":index==3?"H1DBG-V141-01":0;
            if(!s){stall(u);return;}
            n=2;while(*s && n+2<=sizeof u->control){u->control[n++]=(uint8_t)*s++;u->control[n++]=0;}
            u->control[0]=(uint8_t)n;u->control[1]=3;
        }
        p=u->control;
    }
    if(!p){stall(u);return;}
    if(n>requested)n=requested;
    if(p!=u->control)for(unsigned i=0;i<n;++i)u->control[i]=p[i];
    u->control_length=(uint16_t)n;u->control_offset=0;u->control_phase=1;
    R16(U+0x12)=0x40;send_control(u);
}
static void setup(debug_usb *u,const uint8_t s[8])
{
    unsigned value=le16(s+2),index=le16(s+4),length=le16(s+6);++u->setups;
    u->control_phase=0;
    if(s[0]==0x80 && s[1]==6){descriptor(u,value>>8,value&255,length);return;}
    if(s[0]==0 && s[1]==5 && !index && !length && value<=127){
        /* JZ4740 FADDR UPDATE delays activation until status completes. */
        R8(U)=value;status(u);return;
    }
    if(s[0]==0 && s[1]==9 && !index && !length && value<=1){
        u->configured=(uint8_t)value;endpoints();status(u);return;
    }
    if(s[0]==0x80 && s[1]==8 && !value && !index && length==1){
        u->control[0]=u->configured;u->control_length=1;u->control_offset=0;R16(U+0x12)=0x40;send_control(u);return;
    }
    if((s[0]==0x80 || s[0]==0x81 || s[0]==0x82) && s[1]==0 && !value && length==2){
        u->control[0]=u->control[1]=0;u->control_length=2;u->control_offset=0;R16(U+0x12)=0x40;send_control(u);return;
    }
    if(s[0]==0x81 && s[1]==10 && index<2 && !value && length==1){
        u->control[0]=0;u->control_length=1;u->control_offset=0;R16(U+0x12)=0x40;send_control(u);return;
    }
    if(s[0]==1 && s[1]==11 && index<2 && !value && !length){status(u);return;}
    if(s[0]==2 && s[1]==1 && !value && !length && (index==1 || index==0x81 || index==0x82)){
        R8(U+0x0e)=(uint8_t)(index&15);
        if(index&128){flush_in();R16(U+0x12)=0x2040;}
        else {flush_out();R16(U+0x16)=0x80;}
        R8(U+0x0e)=0;status(u);return;
    }
    if(s[0]==0x21 && s[1]==0x20 && !value && !index && length==7){
        u->control_phase=2;R16(U+0x12)=0x40;return;
    }
    if(s[0]==0xa1 && s[1]==0x21 && !value && !index && length==7){
        for(unsigned i=0;i<7;++i)u->control[i]=u->line[i];
        u->control_length=7;u->control_offset=0;R16(U+0x12)=0x40;send_control(u);return;
    }
    if(s[0]==0x21 && s[1]==0x22 && !index && !length && !(value&~3u)){
        if((u->control_lines^value)&1u){++u->session_epoch;flush_session();}
        u->control_lines=(uint16_t)value;status(u);return;
    }
    if(s[0]==0x21 && s[1]==0x23 && !index && !length){status(u);return;}
    stall(u);
}
int debug_usb_open(debug_usb *u)
{
    h1_memset(u,0,sizeof *u);
    if(!fw_matches())return 1;
    /* Guard stock USB initializer and IRQ worker, not just RF code. */
    if(R32(0x800615d0)!=0x27bdffe8 || R32(0x80062104)!=0x27bdffe8 ||
       R32(0x800623e0)!=0x27bdffe8)return 1;
    /* Refuse takeover of an enumerated/connected disk session. */
    if(debug_usb_cable_connected())return 2;
    uint32_t irq=lock();
    u->clock_gate=R32(0xb0000020);u->opcr=R32(0xb0000024);
    u->imr=R32(0xb0001004);u->pdmask=R32(0xb0010320);
    u->pdflag=R32(0xb0010380);
    R32(0xb0001008)=USB_IRQ;R32(0xb0010324)=PD6;
    R32(0xb0000020)&=~(1u<<11);R32(0xb0000024)|=0x40;
    u->power=R8(U+1);u->index=R8(U+0xe);u->txe=R16(U+6);u->rxe=R16(U+8);u->usbe=R8(U+0xb);u->address=R8(U);
    R8(U+1)=0;R16(U+6)=0;R16(U+8)=0;R8(U+0xb)=0;
    for(unsigned i=0;i<3;++i){R8(U+0xe)=(uint8_t)i;
        u->maxp[i][0]=R16(U+0x10);u->maxp[i][1]=R16(U+0x14);
        u->csr[i][0]=R16(U+0x12);u->csr[i][1]=R16(U+0x16);
    }
    u->owned=1;R8(U)=0;endpoints();R16(U+0x12)=0xc0;
    u->line[0]=0;u->line[1]=0xc2;u->line[2]=1;u->line[6]=8;
    R8(U+1)=0x40;unlock(irq);return 0;
}
void debug_usb_poll(debug_usb *u)
{
    if(!u->owned)return;
    uint8_t events=R8(U+0xa);
    uint8_t power=R8(U+1);
    unsigned reset_rechecked=0;
check_reset:
    if((events&4) || (power&8) || u->reset_pending){
        /* JZ4740 PM 21.6: a USB bus reset ENABLES endpoint interrupts.
         * This is hardware, not a native task taking ownership. IRQ24 stays
         * masked. Refuse native clock/IRQ/cable service changes nonetheless. */
        if(R8(U+0xb) || !(R32(0xb0001004)&USB_IRQ) ||
           !(R32(0xb0010320)&PD6) || (power&0x20)){
            ownership_error(u,events);return;
        }
        if(!u->reset_pending){
            u->reset_pending=1;u->configured=0;u->control_phase=0;++u->resets;
            u->reset_tick=fw_tick();
        }
        R16(U+6)=0;R16(U+8)=0;
        /* POWER.Reset can rise before INTRUSB.Reset is latched. During
         * SE0 the endpoint IRQ enables have already returned to defaults.
         * Keep ownership masks, pause service and finish after reset ends. */
        if(power&8){
            if(fw_tick()-u->reset_tick>200u){ownership_error(u,events);u->fault_reason|=64;return;}
            (void)R16(U+2);(void)R16(U+4);return;
        }
        u->reset_pending=0;R8(U)=0;endpoints();
    }
    /* Catch a native task re-enabling controller service rather than race it. */
    if(R16(U+6) || R16(U+8) || R8(U+0xb)){
        /* Reset can start between the POWER/INTRUSB snapshot above and
         * this endpoint-enable read. H1UD01 on v0.5 captured POWER=48,
         * TXE=7/RXE=2 despite the earlier snapshot showing no reset.
         * Re-sample once before declaring ownership loss. A real reset
         * still passes the native-service/IRQ/HS guards above; without
         * reset evidence the original fail-closed path remains intact. */
        if(!reset_rechecked){
            reset_rechecked=1;events|=R8(U+0xa);power=R8(U+1);
            if((events&4) || (power&8) || u->reset_pending)goto check_reset;
        }
        ownership_error(u,events);return;
    }
    if(!(R32(0xb0001004)&USB_IRQ) || !(R32(0xb0010320)&PD6) ||
       (R8(U+1)&0x20) || (R32(0xb0000020)&(1u<<11)) || !(R32(0xb0000024)&0x40u)){
        ownership_error(u,events);u->fault_reason|=128u;return;
    }
    (void)R16(U+2);(void)R16(U+4);
    R8(U+0xe)=0;
    unsigned csr=R16(U+0x12);
    if(csr&4){R16(U+0x12)=0;u->control_phase=0;return;}
    if(csr&0x10){R16(U+0x12)=0x80;u->control_phase=0;csr=R16(U+0x12);}
    if(csr&1){
        unsigned n=R16(U+0x18);
        if(u->control_phase==2 && n==7){for(unsigned i=0;i<7;++i)u->line[i]=R8(U+0x20);status(u);}
        else if(n==8){uint8_t s[8];for(unsigned i=0;i<8;++i)s[i]=R8(U+0x20);setup(u,s);}
        else if(!n){R16(U+0x12)=0x40;u->control_phase=0;}
        else {for(unsigned i=0;i<n && i<64;++i)(void)R8(U+0x20);stall(u);}
    }else if(u->control_phase==1 && !(csr&2))send_control(u);
}
int debug_usb_read(debug_usb *u,uint8_t *data,unsigned capacity)
{
    if(!u->configured)return 0;R8(U+0xe)=1;
    unsigned csr=R16(U+0x16);if(!(csr&1))return 0;
    unsigned n=R16(U+0x18);if(n>64 || n>capacity)return -1;
    for(unsigned i=0;i<n;++i)data[i]=R8(U+0x24);
    R16(U+0x16)=0;++u->rx_packets;return (int)n;
}
int debug_usb_write(debug_usb *u,const uint8_t *data,unsigned length)
{
    if(!u->configured || length>64)return 0;R8(U+0xe)=1;
    if(R16(U+0x12)&3)return 0;
    for(unsigned i=0;i<length;++i)R8(U+0x24)=data[i];
    R16(U+0x12)=0x2001;++u->tx_packets;return 1;
}
int debug_usb_tx_idle(debug_usb *u){(void)u;R8(U+0xe)=1;return !(R16(U+0x12)&3);}
void debug_usb_close(debug_usb *u)
{
    if(!u->owned)return;R8(U+1)=0;
    /* Give the host a real disconnect before restoring its disk descriptors.
     * Scheduler IRQs stay enabled, and a stopped firmware clock is bounded. */
    uint32_t start=fw_tick();
    for(unsigned n=0;n<100u && fw_tick()-start<20u;++n)fw_delay(1);
    uint32_t irq=lock();
    R16(U+6)=0;R16(U+8)=0;R8(U+0xb)=0;
    for(unsigned i=0;i<3;++i){R8(U+0xe)=(uint8_t)i;
        if(i){flush_in();flush_out();R16(U+0x12)=0x40;R16(U+0x16)=0x80;}
        R16(U+0x10)=u->maxp[i][0];R16(U+0x14)=u->maxp[i][1];
        if(i){R16(U+0x12)=u->csr[i][0]&0xfc50;R16(U+0x16)=u->csr[i][1]&0xf8a0;}
    }
    R8(U+0xe)=u->index;R8(U)=u->address&127;
    (void)R8(U+0xa);(void)R16(U+2);(void)R16(U+4);
    R16(U+6)=u->txe;R16(U+8)=u->rxe;R8(U+0xb)=u->usbe;
    R8(U+1)=u->power&0xe1;
    R32(0xb0000024)=(R32(0xb0000024)&~0x40u)|(u->opcr&0x40u);
    R32(0xb0000020)=(R32(0xb0000020)&~(1u<<11))|(u->clock_gate&(1u<<11));
    /* Clear only the cable edge generated while its IRQ was masked. */
    if(!(u->pdflag&PD6))R32(0xb0010314)=PD6;
    R32((u->pdmask&PD6)?0xb0010324:0xb0010328)=PD6;
    R32((u->imr&USB_IRQ)?0xb0001008:0xb000100c)=USB_IRQ;
    u->owned=0;unlock(irq);
}

