/* A cancelable test loop. poll_cancel also gives foreground applications
 * execution time. Use raw ticks or separately calibrated wall time. */
#include "module_abi.h"
int h1_module_entry(const h1_exec_api *api,h1_exec_call *call)
{
    uint32_t loops=0,begin=api->tick_80hz();
    while(!api->poll_cancel())++loops;
    uint32_t *out=(uint32_t *)(void *)call->output;
    out[0]=loops;out[1]=begin;out[2]=api->tick_80hz();call->output_size=12;
    return -2;
}
