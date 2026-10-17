#ifndef H1_DEBUG_SCREEN_CODEC_H
#define H1_DEBUG_SCREEN_CODEC_H
#include <stdint.h>

/* Encode a prefix of an immutable 2/4-byte pixel buffer without changing any
 * pixel bits. Literal tag 0..127 carries 1..128 pixels. Repeat tag 128..255
 * carries 2..129 copies of its one following pixel. Returned byte count fits
 * capacity; consumed is positive for a valid nonempty input and >=1 pixel of
 * output space. Caller supplies an aligned byte_count, not an untrusted RAM
 * address. No allocation, global state, padding reads or writes. */
unsigned rd_screen_rle_encode(const uint8_t *src,unsigned byte_count,
    unsigned pixel_bytes,uint8_t *out,unsigned capacity,unsigned *consumed);
#endif
