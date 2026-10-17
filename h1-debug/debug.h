#ifndef H1_RESIDENT_DEBUG_H
#define H1_RESIDENT_DEBUG_H
#include <stdint.h>
#define RD_MAGIC 0x31444248u
#define RD_VERSION 1u
#define RD_PRIORITY 6u
#define RD_STACK_BYTES 32768u
enum { RD_INIT=0, RD_RUNNING=1, RD_STOPPING=2, RD_STOPPED=3, RD_FAULT=4 };
typedef struct rd_header rd_header;
struct rd_header {
    uint32_t magic,version,allocation,base,size,priority;
    volatile uint32_t state,stop_request,last_error;
    int (*init)(rd_header *);
    void (*worker)(void *);
    void (*shutdown)(rd_header *);
    uint32_t stack_top,stack_bottom;
    uint32_t runtime_tables[5];
    uint32_t image_id;
};
extern rd_header rd_control;
void rd_cache_sync(void *start,void *end);
#endif
