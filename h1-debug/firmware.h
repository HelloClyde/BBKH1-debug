#ifndef H1_DEBUG_FIRMWARE_H
#define H1_DEBUG_FIRMWARE_H
#include "debug.h"

/* These addresses belong only to H1/Y100 V1.41, project.bin SHA256
 * d05786e442f9aad62a8d0a0cb4f6d786bdc7c2fa353a7a2b152c9ed9f01b40ef.
 * fw_matches checks the original routines before the launcher uses them. */
#define FW_TASK_TABLE 0x80a6d6f0u
#define FW_TASK_CREATE_EXT 0x8000db5cu
#define FW_TASK_DELETE 0x8000dce0u
#define FW_TASK_DELAY 0x8000e8fcu
#define FW_RAW_TICK 0x80165b04u
#define FW_TICK_IRQ_SLOT 0x8057b6d4u

int fw_matches(void);
uint32_t fw_tick(void);
void fw_delay(uint32_t ticks);
int fw_wake_install(rd_header *control);
int fw_wake_remove(void);
int fw_task_create(rd_header *control);
int fw_task_delete(uint32_t priority);
rd_header *fw_task_control(void);
int fw_task_slot_empty(void);
void *fw_alloc(uint32_t bytes);
void fw_free(void *pointer);

#endif
