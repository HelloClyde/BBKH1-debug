/* Finite CPU timing probe. Runs without cooperative sleeps inside the measured
 * interval; foreground execution pauses until this bounded calculation ends. */
#include "module_abi.h"
int h1_module_entry(const h1_exec_api *api,h1_exec_call *call)
{
    uint32_t count=4000000u,value=0x12345678u;
    if(call->arg_size){
        if(call->arg_size!=4)return -1;
        count=call->args[0]|((uint32_t)call->args[1]<<8)|
              ((uint32_t)call->args[2]<<16)|((uint32_t)call->args[3]<<24);
    }
    if(!count || count>20000000u)return -2;
    uint32_t begin=api->tick_80hz();
    for(uint32_t i=0;i<count;++i)value=(value<<7)^(value>>3)^(i+0x9e3779b9u);
    uint32_t end=api->tick_80hz();
    uint32_t *out=(uint32_t *)(void *)call->output;
    out[0]=count;out[1]=value;out[2]=begin;out[3]=end;call->output_size=16;
    return 0;
}
