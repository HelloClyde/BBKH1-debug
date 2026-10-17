#include "native_exec.h"
#include "../h1-usb-debug/protocol.h"
/* Static RAM reservations remain inside the BDA's linker-checked 4 MiB
 * loading window. No heap guess, flash write or persistent firmware patch.
 */
#ifdef H1_NETWORK_AUTONOMOUS
static uint8_t module_arena[H1_EXEC_IMAGE_MAX] __attribute__((aligned(32),section(".h1_module")));
#else
static uint8_t module_arena[H1_EXEC_IMAGE_MAX] __attribute__((aligned(32)));
#endif
static uint8_t module_scratch[H1_EXEC_SCRATCH_MAX+64] __attribute__((aligned(32)));
static uint8_t module_stack[H1_EXEC_STACK_MAX+64] __attribute__((aligned(32)));
static uint8_t module_output[H1_EXEC_OUTPUT_MAX+64] __attribute__((aligned(32)));
static uint8_t module_args[256];
static h1_exec_call call;
exec_diagnostics exec_diag;
uint32_t h1_exec_saved_sp;
extern int h1_exec_trampoline(h1_module_fn,const h1_exec_api *,h1_exec_call *,void *);
static void cache_sync(void *start,void *end)
{
    /* Same hit operations/16-byte coverage as SDK v1_game_stage and JIT.
     * Write back/invalidate D before invalidating I; no global cache reset. */
    uintptr_t first=(uintptr_t)start&~15u,limit=((uintptr_t)end+15u)&~15u;
    __asm__ volatile("sync" ::: "memory");
    for(uintptr_t p=first;p<limit;p+=16u)__asm__ volatile("cache 0x15,0(%0)" :: "r"(p):"memory");
    __asm__ volatile("sync" ::: "memory");
    for(uintptr_t p=first;p<limit;p+=16u)__asm__ volatile("cache 0x10,0(%0)" :: "r"(p):"memory");
    __asm__ volatile("sync\n\tnop\n\tnop\n\tnop" ::: "memory");
}
static void guards(uint8_t *p,unsigned size)
{
    for(unsigned i=0;i<32;++i){p[i]=0xa5;p[32+size+i]=0x5a;}
}
static int guard_ok(const uint8_t *p,unsigned size)
{
    for(unsigned i=0;i<32;++i)if(p[i]!=0xa5 || p[32+size+i]!=0x5a)return 0;
    return 1;
}
int exec_active(void){return exec_diag.state==EXEC_PENDING || exec_diag.state==EXEC_RUNNING;}
void exec_info(uint32_t *v)
{
    uint32_t values[]={H1_EXEC_ABI,(uint32_t)(uintptr_t)module_arena,H1_EXEC_IMAGE_MAX,
        H1_EXEC_SCRATCH_MAX,H1_EXEC_STACK_MAX,H1_EXEC_OUTPUT_MAX,exec_diag.state,
        exec_diag.image_size,exec_diag.memory_size,exec_diag.entry_offset,exec_diag.upload_crc,
        exec_diag.written,exec_diag.runs,exec_diag.flags,0 /* no forced preemption */,sizeof(h1_exec_api)};
    for(unsigned i=0;i<16;++i)v[i]=values[i];
}
int exec_begin(uint32_t image,uint32_t memory,uint32_t entry,uint32_t crc,uint32_t abi)
{
    if(exec_active())return 2;
    if(abi!=H1_EXEC_ABI || image<4 || image>H1_EXEC_IMAGE_MAX ||
       memory<image || memory>H1_EXEC_IMAGE_MAX || entry%4 || entry>image-4)return 1;
    exec_diag=(exec_diagnostics){.state=EXEC_LOADING,.image_size=image,.memory_size=memory,
        .entry_offset=entry,.upload_crc=crc};return 0;
}
int exec_write(uint32_t offset,const uint8_t *data,unsigned count)
{
    if(exec_diag.state!=EXEC_LOADING)return 2;
    if(!count || count>508 || offset!=exec_diag.written || offset>exec_diag.image_size || count>exec_diag.image_size-offset)return 1;
    for(unsigned i=0;i<count;++i)module_arena[offset+i]=data[i];
    exec_diag.written+=count;return 0;
}
int exec_commit(void)
{
    if(exec_diag.state!=EXEC_LOADING)return 2;
    if(exec_diag.written!=exec_diag.image_size || debug_crc32(module_arena,exec_diag.image_size)!=exec_diag.upload_crc)return 1;
    for(unsigned i=exec_diag.image_size;i<exec_diag.memory_size;++i)module_arena[i]=0;
    for(unsigned i=0;i<H1_EXEC_SCRATCH_MAX;++i)module_scratch[32+i]=0;
    cache_sync(module_arena,module_arena+exec_diag.memory_size);
    exec_diag.state=EXEC_READY;return 0;
}
int exec_schedule(unsigned opcode,unsigned budget,const uint8_t *args,unsigned count)
{
    if(exec_diag.state!=EXEC_READY && exec_diag.state!=EXEC_DONE)return 2;
    if(count>256 || budget<1 || budget>400)return 1;
    for(unsigned i=0;i<count;++i)module_args[i]=args[i];
    exec_diag.opcode=opcode;exec_diag.budget_ticks=budget;exec_diag.arg_size=count;
    exec_diag.output_size=0;exec_diag.flags=0;exec_diag.return_value=0;
    exec_diag.state=EXEC_PENDING;return 0;
}
void exec_request_cancel(unsigned reason)
{
    if(exec_active())exec_diag.flags|=reason;
}
void exec_invoke(const h1_exec_api *api)
{
    if(exec_diag.state!=EXEC_PENDING)return;
    if(exec_diag.flags){exec_diag.state=EXEC_DONE;exec_diag.return_value=-2;return;}
    guards(module_scratch,H1_EXEC_SCRATCH_MAX);guards(module_stack,H1_EXEC_STACK_MAX);guards(module_output,H1_EXEC_OUTPUT_MAX);
    call=(h1_exec_call){.version=H1_EXEC_ABI,.size=sizeof call,.opcode=exec_diag.opcode,
        .arg_size=exec_diag.arg_size,.args=module_args,.output=module_output+32,.output_capacity=H1_EXEC_OUTPUT_MAX,
        .scratch=module_scratch+32,.scratch_capacity=H1_EXEC_SCRATCH_MAX};
    exec_diag.state=EXEC_RUNNING;exec_diag.start_tick=api->tick_80hz();++exec_diag.runs;
    h1_module_fn fn=(h1_module_fn)(module_arena+exec_diag.entry_offset);
    exec_diag.return_value=h1_exec_trampoline(fn,api,&call,module_stack+32+H1_EXEC_STACK_MAX);
    exec_diag.end_tick=api->tick_80hz();
    if(exec_diag.end_tick-exec_diag.start_tick>=exec_diag.budget_ticks)exec_diag.flags|=EXEC_TIMEOUT;
    if(!guard_ok(module_scratch,H1_EXEC_SCRATCH_MAX) || !guard_ok(module_stack,H1_EXEC_STACK_MAX) ||
       !guard_ok(module_output,H1_EXEC_OUTPUT_MAX))exec_diag.flags|=EXEC_GUARD;
    if(call.output_size>H1_EXEC_OUTPUT_MAX)exec_diag.flags|=EXEC_OUTPUT;
    exec_diag.output_size=exec_diag.flags&(EXEC_GUARD|EXEC_OUTPUT)?0:call.output_size;
    exec_diag.state=EXEC_DONE;
}
unsigned exec_read(unsigned offset,unsigned wanted,uint8_t *out)
{
    if(exec_diag.state!=EXEC_DONE || !wanted || wanted>504 || offset>exec_diag.output_size)return 0;
    unsigned n=exec_diag.output_size-offset;if(n>wanted)n=wanted;
    for(unsigned i=0;i<n;++i)out[i]=module_output[32+offset+i];return n;
}
int exec_reset(void)
{
    if(exec_active())return 2;
    exec_diag=(exec_diagnostics){0};return 0;
}
