#ifndef H1_MODULE_ABI_H
#define H1_MODULE_ABI_H
#include <stdint.h>
#define H1_EXEC_ABI 1u
#ifndef H1_EXEC_IMAGE_MAX
#define H1_EXEC_IMAGE_MAX 65536u
#endif
#define H1_EXEC_SCRATCH_MAX 16384u
#define H1_EXEC_STACK_MAX 32768u
#define H1_EXEC_OUTPUT_MAX 4096u
/* Trusted native code ABI, NOT a sandbox. Arbitrary MIPS instructions/MMIO
 * remain possible. A module must return or poll cooperatively; an unbounded
 * loop or hardware corruption cannot be forcibly recovered by this host.
 */
typedef struct {
    uint32_t version,size;
    uint32_t (*tick_80hz)(void);
    int (*poll_cancel)(void);
    unsigned (*device_command)(unsigned,const uint8_t *,unsigned,uint8_t *,unsigned *);
    uint32_t (*runtime_table)(unsigned);
    uint32_t (*runtime_entry)(uint32_t,unsigned);
} h1_exec_api;
typedef struct {
    uint32_t version,size,opcode,arg_size;
    const uint8_t *args;
    uint8_t *output;
    uint32_t output_capacity,output_size;
    uint8_t *scratch;
    uint32_t scratch_capacity;
} h1_exec_call;
typedef int (*h1_module_fn)(const h1_exec_api *,h1_exec_call *);
#endif
