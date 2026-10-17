#ifndef H1_DEBUG_SHELL_H
#define H1_DEBUG_SHELL_H
#include <stdint.h>
#define RD_SHELL_PATH_MAX 240u
enum { RD_SHELL_REFRESH=1,RD_SHELL_RUN=2 };
enum { RD_SHELL_IDLE=0,RD_SHELL_QUEUED,RD_SHELL_LOADING,RD_SHELL_RUNNING,
       RD_SHELL_DONE,RD_SHELL_FAILED };
enum { RD_SHELL_OK=0,RD_SHELL_INVALID,RD_SHELL_BUSY,RD_SHELL_BINDING,
       RD_SHELL_NOT_FOUND,RD_SHELL_BDA_INVALID,RD_SHELL_LOAD_FAILED,
       RD_SHELL_EXPIRED,RD_SHELL_CONTEXT,RD_SHELL_IO };
/* init/check/shutdown: zero is success. Only init/shutdown change bindings.
 * A request merely publishes a job. The desktop's existing timer dispatch
 * performs it in GUI task 10, with no cross-task GUI queue manipulation.
 * Caller must exclude file commits and native EXEC during QUEUED/LOADING.
 * Once the normal BDA is RUNNING, ordinary background debugging can resume.
 * Shutdown refuses QUEUED/LOADING. RUNNING may detach its bindings because the
 * resident allocation and task remain retained until reboot: the foreground
 * payload can still return through its original resident bridge/callback.
 * init/shutdown never reset that active job; it continues to prevent reentry.
 */
int rd_shell_init(void);
int rd_shell_check(void);
int rd_shell_shutdown(void);
int rd_shell_active(void);
int rd_shell_stop_blocked(void);
int rd_shell_request(uint32_t kind,const uint8_t *path,uint32_t length,uint32_t *job_id);
void rd_shell_poll(uint32_t now);
/* job_id,kind,state,error,menu_idle,app_index,entry_count,return_code.
 * entry_count is cumulative, incremented only at the native payload call site.
 * A quick app can already be DONE before the host sees RUNNING. */
void rd_shell_status(uint32_t status[8]);
#endif
