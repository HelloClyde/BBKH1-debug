/* Read-only V1.41 LCD preview, no device/BDA changes. Each call samples at
 * most 18 rows of a 96x54 RGB565 preview from the currently displayed DMA
 * surface. Pixel coordinates are floor(x*480/96), floor(y*272/54).
 *
 * Args (8 LE u32): version=1, expected uncached source, source width/height,
 * source byte stride, source format (1 RGB565 / 2 BGRX8888), first preview
 * row, row count (1..18). Expected source/layout comes from SCREEN_CAPTURE.
 *
 * Output: 16 LE u32 header, then row-major little-endian RGB565 pixels.
 * Header: magic='HPV1', version, source width/height/stride/format/address,
 * physical DMA descriptor, preview width/height, first row, row count,
 * pixel byte count, start/end raw tick, sampling rule=1 (floor scaling).
 * Each tile verifies LCD descriptor/layout before and after reading. A GUI
 * may still draw into that surface; tiles are not an atomic snapshot.
 */
#include "module_abi.h"
#define R32(a) (*(volatile uint32_t *)(uintptr_t)(a))
#define RAM_SIZE 0x04000000u
#define LCD 0xb3050000u
#define WIDTH 96u
#define HEIGHT 54u

static uint32_t get32(const uint8_t *p)
{return p[0]|((uint32_t)p[1]<<8)|((uint32_t)p[2]<<16)|((uint32_t)p[3]<<24);}
static int physical(uint32_t address,uint32_t size)
{return address<RAM_SIZE && size<=RAM_SIZE-address;}
static int firmware(void)
{
 return R32(0x80005b48u)==0x3c02803fu && R32(0x80005b4cu)==0x8c422408u &&
        R32(0x80005824u)==0x3c02803fu && R32(0x80005828u)==0x03e00008u &&
        R32(0x8000582cu)==0x8c422438u;
}

int h1_module_entry(const h1_exec_api *api,h1_exec_call *call)
{
 if(!api || !call || api->version!=1 || api->size<28 || call->version!=1 ||
    call->arg_size!=32 || !call->args || !call->output)return -1;
 call->output_size=0;
 uint32_t arg[8];for(unsigned i=0;i<8;++i)arg[i]=get32(call->args+i*4u);
 uint32_t source_expected=arg[1],width=arg[2],height=arg[3],stride=arg[4],format=arg[5];
 uint32_t first=arg[6],rows=arg[7],bytes=format==1?2u:format==2?4u:0u;
 if(arg[0]!=1 || width!=480 || height!=272 || !bytes || stride!=width*bytes ||
    !rows || rows>18 || first>=HEIGHT || rows>HEIGHT-first ||
    call->output_capacity<64u+rows*WIDTH*2u ||
    (source_expected&0xe0000000u)!=0xa0000000u || (source_expected&3u))return -2;
 uint32_t total=stride*height,source_physical=source_expected&0x1fffffffu;
 if(!physical(source_physical,total) || !firmware())return -3;
 uint32_t bits=R32(0x803f240cu),control=R32(LCD+0x30u);
 if(R32(0x803f2404u)!=width || R32(0x803f2408u)!=height || !(control&8u) ||
    (format==1?bits!=16u:(bits!=18u && bits!=32u)))return -4;
 uint32_t desc=R32(LCD+0x40u)&0x1fffffffu;
 if((desc&15u) || !physical(desc,16))return -5;
 uint32_t descriptor=0xa0000000u|desc;
 uint32_t next=R32(descriptor),source=R32(descriptor+4u),command=R32(descriptor+12u);
 if((next&0x1fffffffu)!=desc || source!=source_physical ||
    (command&0x10000000u) || (command&0x00ffffffu)!=total/4u)return -6;
 uint32_t begin=api->tick_80hz();uint8_t *out=call->output+64u;
 for(uint32_t row=0;row<rows;++row){
  uint32_t y=(first+row)*height/HEIGHT;
  for(uint32_t col=0;col<WIDTH;++col){
   uint32_t x=col*width/WIDTH,address=source_expected+y*stride+x*bytes;
   uint32_t pixel;
   if(bytes==2)pixel=*(volatile uint16_t *)(uintptr_t)address;
   else {
    uint32_t bgrx=R32(address);
    pixel=((bgrx>>8)&0xf800u)|((bgrx>>5)&0x07e0u)|((bgrx>>3)&0x001fu);
   }
   *out++=(uint8_t)pixel;*out++=(uint8_t)(pixel>>8);
  }
 }
 uint32_t end=api->tick_80hz();
 /* No writes to LCD registers, descriptor, source pixels, or allocator. */
 if((R32(LCD+0x40u)&0x1fffffffu)!=desc || R32(descriptor)!=next ||
    R32(descriptor+4u)!=source || R32(descriptor+12u)!=command ||
    R32(0x803f2404u)!=width || R32(0x803f2408u)!=height ||
    R32(0x803f240cu)!=bits || R32(LCD+0x30u)!=control)return -7;
 uint32_t header[16]={0x31565048u,1,width,height,stride,format,source_expected,desc,
                     WIDTH,HEIGHT,first,rows,rows*WIDTH*2u,begin,end,1};
 for(unsigned i=0;i<16;++i)for(unsigned j=0;j<4;++j)call->output[i*4u+j]=(uint8_t)(header[i]>>(8u*j));
 call->output_size=64u+rows*WIDTH*2u;return 0;
}
