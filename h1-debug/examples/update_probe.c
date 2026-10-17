/* A normal BDA, not an EXEC module. Two builds prove desktop metadata refresh
 * and loading replacement machine code while the debugger remains resident. */
#include "h1_sdk.h"
#ifndef UPDATE_PROBE_VERSION
#define UPDATE_PROBE_VERSION 1
#endif

volatile unsigned update_probe_identity[4] = {
    0x31504455u, UPDATE_PROBE_VERSION, 0, 0
};
static h1_u16 scanline[480];

static void rectangle(int x,int y,int width,int height,h1_u16 color)
{
    for(int i=0;i<width;i++)scanline[i]=color;
    for(int row=0;row<height;row++)h1_blit_rgb565(x,y+row,width,1,scanline);
}

static void digit(int value,int x,int y,h1_u16 color)
{
    static const unsigned char segments[10]={0x3f,6,0x5b,0x4f,0x66,0x6d,0x7d,7,0x7f,0x6f};
    unsigned mask=segments[value%10];
    if(mask&1)rectangle(x+10,y,50,10,color);
    if(mask&2)rectangle(x+60,y+10,10,50,color);
    if(mask&4)rectangle(x+60,y+70,10,50,color);
    if(mask&8)rectangle(x+10,y+120,50,10,color);
    if(mask&16)rectangle(x,y+70,10,50,color);
    if(mask&32)rectangle(x,y+10,10,50,color);
    if(mask&64)rectangle(x+10,y+60,50,10,color);
}

int h1_app_main(void)
{
    h1_u16 background=UPDATE_PROBE_VERSION==1?0x0192:0x0328;
    rectangle(0,0,480,272,background);
    rectangle(16,16,448,6,0xffff);
    rectangle(16,250,448,6,0xffff);
    digit(UPDATE_PROBE_VERSION,205,55,0xffff);
    h1_present_full_screen();
    unsigned last=h1_raw_tick_80hz(),phase=0;
    for(;;){
        int code=-1,key=-1;
        h1_event_fetch(&code,&key);
        if(code==H1_EVENT_KEY_DOWN){
            update_probe_identity[3]=(unsigned)key;
            if(key==H1_KEY_ESCAPE || key==H1_KEY_BACK)return 0;
        }
        unsigned now=h1_raw_tick_80hz();
        if(now-last>=10u){
            last=now;++phase;update_probe_identity[2]=phase;
            rectangle(30,215,420,12,background);
            rectangle(30+(int)(phase%20u)*21,215,20,12,0xffe0);
            h1_present_full_screen();
        }
    }
}
