/* Original compute probe. Uploading a changed constant demonstrates that
 * instruction-cache replacement works without reinstalling the BDA. */
#include "module_abi.h"
static uint32_t runs;
int h1_module_entry(const h1_exec_api *api,h1_exec_call *call)
{
    uint32_t value=0x13579bdfu;
    for(unsigned i=0;i<call->arg_size;++i)value=(value<<5)^(value>>2)^call->args[i];
    uint32_t *out=(uint32_t *)(void *)call->output;
    out[0]=++runs;out[1]=value;out[2]=call->opcode;out[3]=api->tick_80hz();
    call->output_size=16;return 0;
}
