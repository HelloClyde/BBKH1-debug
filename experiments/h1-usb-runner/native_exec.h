#ifndef H1_NATIVE_EXEC_H
#define H1_NATIVE_EXEC_H
#include "module_abi.h"
enum { EXEC_EMPTY=0,EXEC_LOADING=1,EXEC_READY=2,EXEC_PENDING=3,EXEC_RUNNING=4,EXEC_DONE=5 };
enum { EXEC_USER_CANCEL=1,EXEC_TIMEOUT=2,EXEC_USB_FAULT=4,EXEC_GUARD=8,EXEC_OUTPUT=16,EXEC_RESTORE=32 };
typedef struct {
    uint32_t state,image_size,memory_size,entry_offset,upload_crc,written;
    uint32_t runs,flags,start_tick,end_tick,budget_ticks,opcode,arg_size,output_size;
    int32_t return_value;
} exec_diagnostics;
extern exec_diagnostics exec_diag;
int exec_active(void);
void exec_info(uint32_t *values);
int exec_begin(uint32_t image,uint32_t memory,uint32_t entry,uint32_t crc,uint32_t abi);
int exec_write(uint32_t offset,const uint8_t *data,unsigned count);
int exec_commit(void);
int exec_schedule(unsigned opcode,unsigned budget,const uint8_t *args,unsigned count);
void exec_request_cancel(unsigned reason);
void exec_invoke(const h1_exec_api *api);
unsigned exec_read(unsigned offset,unsigned wanted,uint8_t *out);
int exec_reset(void);
#endif
