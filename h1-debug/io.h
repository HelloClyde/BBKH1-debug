#ifndef H1_RESIDENT_DEBUG_IO_H
#define H1_RESIDENT_DEBUG_IO_H
#include <stdint.h>

/* V1.41 only. Matrix key N uses bit N-1; only keys 1..42 are supported.
 * The resident image must stay allocated after shutdown: the firmware queue
 * retains pointers to immutable key records until its consumers finish. */
int rd_io_init(void);
int rd_io_shutdown(void);
int rd_keys_set(uint32_t low, uint32_t high, uint32_t lease_ticks, uint32_t now);
void rd_keys_poll(uint32_t now);
void rd_keys_get(uint32_t *low, uint32_t *high);
/* Single virtual pen in calibrated LCD pixels. Down/move require a finite
 * 1..4000 tick lease. Up ignores x/y and retains the last accepted position.
 * Status is x,y,pressed,lease_remaining. Queue pressure returns 2; an up is
 * still scheduled for retry. Physical ADC-only consumers are not virtualized. */
int rd_touch_set(uint32_t x,uint32_t y,uint32_t pressed,uint32_t lease_ticks,uint32_t now);
void rd_touch_get(uint32_t status[4],uint32_t now);
void rd_touch_poll(uint32_t now);
/* metadata: width,height,stride,format,total,source; 1=RGB565,2=BGRX8888.
 * Copies the displayed DMA surface without calling or changing GUI state.
 * A drawing application can update pixels during the copy (possible tearing). */
int rd_screen_capture(uint8_t *dest,uint32_t capacity,uint32_t metadata[6]);
#endif
