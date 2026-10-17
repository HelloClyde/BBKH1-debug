/* Small foreground BDA: install once, toggle the independent resident task. */
#include "debug.h"
#include "firmware.h"
#include "resident_image.h"
#include "h1_sdk.h"

static const char title[]="H1 \xb5\xf7\xca\xd4";
static const char started[]=
    "\xb5\xf7\xca\xd4\xd2\xd1\xd4\xda\xba\xf3\xcc\xa8\xc6\xf4\xb6\xaf\xa1\xa3\n"
    "\xb7\xb5\xbb\xd8\xb2\xcb\xb5\xa5\xba\xf3\xbf\xc9\xd4\xcb\xd0\xd0\xc6\xe4\xcb\xfb\xb3\xcc\xd0\xf2\xa1\xa3\n"
    "\xd4\xd9\xb4\xce\xb4\xf2\xbf\xaa\xb1\xbe\xb3\xcc\xd0\xf2\xbf\xc9\xb9\xd8\xb1\xd5\xb5\xf7\xca\xd4\xa1\xa3";
static const char stopped[]=
    "\xb5\xf7\xca\xd4\xd2\xd1\xb9\xd8\xb1\xd5\xa3\xacUSB\xba\xcd\xca\xe4\xc8\xeb\xbd\xd3\xbf\xda\xd2\xd1\xbb\xd6\xb8\xb4\xa1\xa3\n"
    "\xd7\xa4\xc1\xf4\xc4\xda\xb4\xe6\xb1\xa3\xc1\xf4\xb9\xa9\xcf\xc2\xb4\xce\xc6\xf4\xb6\xaf\xa3\xac\xd6\xd8\xc6\xf4\xba\xf3\xca\xcd\xb7\xc5\xa1\xa3";
static const char incompatible[]=
    "\xb9\xcc\xbc\xfe\xb2\xbb\xbc\xe6\xc8\xdd\xa3\xac\xce\xb4\xc6\xf4\xb6\xaf\xb5\xf7\xca\xd4\xa1\xa3";
static const char occupied[]=
    "\xcf\xb5\xcd\xb3\xc8\xce\xce\xf1\xb2\xdb\xd2\xd1\xb1\xbb\xd5\xbc\xd3\xc3\xa3\xac\xce\xb4\xd0\xde\xb8\xc4\xb8\xc3\xc8\xce\xce\xf1\xa1\xa3";
static const char memory_error[]=
    "\xc4\xda\xb4\xe6\xb2\xbb\xd7\xe3\xa3\xac\xce\xb4\xc6\xf4\xb6\xaf\xb5\xf7\xca\xd4\xa1\xa3";
static const char start_error[]=
    "\xc6\xf4\xb6\xaf\xca\xa7\xb0\xdc\xa3\xac\xc7\xeb\xb0\xce\xb5\xf4USB\xcf\xdf\xba\xf3\xd6\xd8\xca\xd4\xa1\xa3";
static const char stop_error[]=
    "\xb9\xd8\xb1\xd5\xce\xb4\xcd\xea\xb3\xc9\xa3\xac\xb5\xf7\xca\xd4\xc4\xda\xb4\xe6\xc8\xd4\xb1\xa3\xc1\xf4\xa1\xa3\n"
    "\xc7\xeb\xb5\xc8\xb4\xfd\xb5\xb1\xc7\xb0\xb2\xd9\xd7\xf7\xbd\xe1\xca\xf8\xba\xf3\xd6\xd8\xca\xd4\xa1\xa3";
static const char version_error[]=
    "\xd7\xa4\xc1\xf4\xb0\xe6\xb1\xbe\xd3\xeb\xb0\xb2\xd7\xb0\xb0\xfc\xb2\xbb\xcd\xac\xa3\xac\xc7\xeb\xd6\xd8\xc6\xf4\xc9\xe8\xb1\xb8\xba\xf3\xd4\xd9\xc6\xf4\xb6\xaf\xa1\xa3";

static int notice(const char *message,int error)
{
    char buffer[280];unsigned n=0;
    while(message[n] && n<sizeof buffer-32u){buffer[n]=message[n];n++;}
    if(error){
        static const char hex[]="0123456789ABCDEF";
        buffer[n++]='\n';buffer[n++]='[';buffer[n++]='0';buffer[n++]='x';
        for(int i=7;i>=0;i--)buffer[n++]=hex[((uint32_t)error>>(i*4))&15u];
        buffer[n++]=']';
    }
    buffer[n]=0;h1_message_box(0,buffer,title,0);return error;
}

static void sync_code(uint32_t base,uint32_t bytes)
{
    __asm__ volatile("sync":::"memory");
    for(uint32_t a=base&~15u;a<base+bytes;a+=16)
        __asm__ volatile("cache 0x15,0(%0)"::"r"(a):"memory");
    __asm__ volatile("sync":::"memory");
    for(uint32_t a=base&~15u;a<base+bytes;a+=16)
        __asm__ volatile("cache 0x10,0(%0)"::"r"(a):"memory");
    __asm__ volatile("sync\n\tnop\n\tnop\n\tnop":::"memory");
}

static int relocate(uint32_t base)
{
    uint32_t delta=base-RD_IMAGE_LINK_BASE;
    for(unsigned i=0;i<RD_RELOC_COUNT;i++){
        const struct rd_reloc *r=&rd_relocations[i];
        if((r->offset&3u) || r->offset>RD_IMAGE_SIZE-4u ||
           r->value<RD_IMAGE_LINK_BASE || r->value-RD_IMAGE_LINK_BASE>RD_MEMORY_SIZE)return 0;
        uint32_t *p=(uint32_t *)(uintptr_t)(base+r->offset),value=r->value+delta;
        switch(r->type){
        case 1:*p=value;break;
        case 2:*p=(*p&0xfc000000u)|((value>>2)&0x03ffffffu);break;
        case 3:*p=(*p&0xffff0000u)|(((value+0x8000u)>>16)&0xffffu);break;
        case 4:*p=(*p&0xffff0000u)|(value&0xffffu);break;
        default:return 0;
        }
    }
    return 1;
}

static int start(rd_header *h)
{
    h->stop_request=0;h->last_error=0;h->state=RD_INIT;
    int rc=h->init(h);
    if(rc){
        h->shutdown(h);
        if(h->state!=RD_STOPPED)h->state=RD_FAULT;
        if(!h->last_error)h->last_error=(uint32_t)rc;
        return notice(start_error,rc);
    }
    h->state=RD_RUNNING;
    return notice(started,0);
}

int h1_app_main(void)
{
    if(!fw_matches())return notice(incompatible,1);
    rd_header *h=fw_task_control();
    if(h){
        if(h->size!=RD_MEMORY_SIZE || h->image_id!=RD_IMAGE_ID)return notice(version_error,2);
        if(h->state==RD_STOPPED)return start(h);
        h->stop_request=1;
        /* Bounded by both observed GUI ticks and iterations. A module which
         * never yields cannot be forcibly unwound by a cooperative service. */
        uint32_t begin=fw_tick();
        for(unsigned i=0;i<400u && h->state!=RD_STOPPED;i++){
            if(fw_tick()-begin>400u)break;
            fw_delay(1);
        }
        if(h->state!=RD_STOPPED)return notice(stop_error,(int)(h->last_error?h->last_error:3u));
        return notice(stopped,0);
    }
    if(!fw_task_slot_empty())return notice(occupied,4);
    if(RD_IMAGE_SIZE<sizeof(rd_header) || RD_IMAGE_SIZE>RD_MEMORY_SIZE || RD_MEMORY_SIZE>0x400000u)
        return notice(version_error,5);
    void *allocation=fw_alloc(RD_MEMORY_SIZE+31u);
    if(!allocation)return notice(memory_error,6);
    uint32_t base=((uint32_t)(uintptr_t)allocation+31u)&~31u;
    h1_memset((void *)(uintptr_t)base,0,RD_MEMORY_SIZE);
    h1_memcpy((void *)(uintptr_t)base,rd_image,RD_IMAGE_SIZE);
    if(!relocate(base)){
        fw_free(allocation);return notice(version_error,7);
    }
    h=(rd_header *)(uintptr_t)base;
    if(h->magic!=RD_MAGIC || h->version!=RD_VERSION || h->image_id!=RD_IMAGE_ID){
        fw_free(allocation);return notice(version_error,8);
    }
    h->allocation=(uint32_t)(uintptr_t)allocation;h->base=base;h->size=RD_MEMORY_SIZE;
    h->priority=RD_PRIORITY;h->state=RD_INIT;h->stop_request=0;h->last_error=0;
    for(unsigned i=0;i<5;i++)h->runtime_tables[i]=*(volatile uint32_t *)(uintptr_t)(0x83c00004u+i*4u);
    sync_code(base,RD_IMAGE_SIZE);
    /* Register first, while worker sleeps in INIT. No device state has yet
     * changed, so task-create failure can safely release the allocation. */
    int rc=fw_task_create(h);
    if(rc){fw_free(allocation);return notice(occupied,rc);}
    return start(h);
}
