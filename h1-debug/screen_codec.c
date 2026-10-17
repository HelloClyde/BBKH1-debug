#include "screen_codec.h"

static int pixel_equal(const uint8_t *a,const uint8_t *b,unsigned bytes)
{
    for(unsigned n=0;n<bytes;++n)if(a[n]!=b[n])return 0;
    return 1;
}

unsigned rd_screen_rle_encode(const uint8_t *src,unsigned byte_count,
    unsigned bytes,uint8_t *out,unsigned capacity,unsigned *consumed)
{
    if(!consumed)return 0;
    *consumed=0;
    if(!src || !out || (bytes!=2u && bytes!=4u) || !byte_count ||
       byte_count%bytes || capacity<1u+bytes)return 0;
    unsigned used=0,input=0;
    while(input<byte_count && capacity-used>=1u+bytes){
        unsigned pixels=(byte_count-input)/bytes;
        const uint8_t *start=src+input;
        unsigned repeat=1;
        while(repeat<129u && repeat<pixels &&
              pixel_equal(start,start+repeat*bytes,bytes))++repeat;
        if(repeat>=2u){
            out[used++]=(uint8_t)(128u+repeat-2u);
            for(unsigned n=0;n<bytes;++n)out[used++]=start[n];
            input+=repeat*bytes;
        }else {
            unsigned limit=(capacity-used-1u)/bytes;
            if(limit>128u)limit=128u;
            if(limit>pixels)limit=pixels;
            unsigned count=1;
            while(count<limit){
                /* Leave the beginning of the next repeated run for its tag. */
                if(count+1u<pixels && pixel_equal(start+count*bytes,
                                                start+(count+1u)*bytes,bytes))break;
                ++count;
            }
            out[used++]=(uint8_t)(count-1u);
            for(unsigned n=0;n<count*bytes;++n)out[used++]=start[n];
            input+=count*bytes;
        }
    }
    *consumed=input;
    return used;
}
