#ifndef H1_DEBUG_PROTOCOL_H
#define H1_DEBUG_PROTOCOL_H
#include <stdint.h>
#define DEBUG_MAX_PAYLOAD 512u
typedef struct {uint8_t rx[528],tx[528];unsigned rx_count,tx_size,tx_offset;uint32_t bad_frames;} debug_protocol;
typedef unsigned (*debug_command_fn)(unsigned opcode,const uint8_t *request,unsigned length,uint8_t *reply,unsigned *status);
void debug_protocol_feed(debug_protocol *p,const uint8_t *data,unsigned length,debug_command_fn command);
uint32_t debug_crc32(const uint8_t *data,unsigned length);
#endif
