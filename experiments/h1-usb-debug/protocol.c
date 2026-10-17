#include "protocol.h"
uint32_t debug_crc32(const uint8_t *data,unsigned length)
{
    uint32_t c=0xffffffff;
    for(unsigned i=0;i<length;++i){c^=data[i];for(unsigned j=0;j<8;++j)c=(c>>1)^(0xedb88320u& (0u-(c&1u)));}
    return ~c;
}
static unsigned get16(const uint8_t *s){return s[0]|((unsigned)s[1]<<8);}
static uint32_t get32(const uint8_t *s){return s[0]|((uint32_t)s[1]<<8)|((uint32_t)s[2]<<16)|((uint32_t)s[3]<<24);}
static void put16(uint8_t *s,unsigned n){s[0]=(uint8_t)n;s[1]=(uint8_t)(n>>8);}
void debug_protocol_feed(debug_protocol *p,const uint8_t *data,unsigned length,debug_command_fn command)
{
    for(unsigned i=0;i<length;++i){
        if(p->rx_count>=sizeof p->rx){p->rx_count=0;++p->bad_frames;}
        p->rx[p->rx_count++]=data[i];
        if(p->rx_count==4 && (p->rx[0]!='H' || p->rx[1]!='1' || p->rx[2]!='D' || p->rx[3]!='B')){
            for(unsigned j=0;j<3;++j)p->rx[j]=p->rx[j+1];p->rx_count=3;continue;
        }
        if(p->rx_count<12)continue;
        unsigned body=get16(p->rx+8),total=16+body;
        if(body>DEBUG_MAX_PAYLOAD || p->rx[4]!=1){p->rx_count=0;++p->bad_frames;continue;}
        if(p->rx_count<total)continue;
        if(get32(p->rx+12+body)!=debug_crc32(p->rx,12+body) || get16(p->rx+10) || (p->rx[5]&128)){
            p->rx_count=0;++p->bad_frames;continue;
        }
        if(p->tx_size){p->rx_count=0;++p->bad_frames;continue;}
        unsigned status=0,n=command(p->rx[5],p->rx+12,body,p->tx+12,&status);
        if(n>DEBUG_MAX_PAYLOAD){n=0;status=1;}
        for(unsigned j=0;j<8;++j)p->tx[j]=p->rx[j];p->tx[5]|=128;
        put16(p->tx+8,n);put16(p->tx+10,status);
        uint32_t crc=debug_crc32(p->tx,12+n);
        for(unsigned j=0;j<4;++j)p->tx[12+n+j]=(uint8_t)(crc>>(j*8));
        p->tx_size=16+n;p->tx_offset=0;p->rx_count=0;
    }
}
