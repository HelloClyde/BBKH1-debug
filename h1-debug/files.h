#ifndef H1_DEBUG_FILES_H
#define H1_DEBUG_FILES_H
#include <stdint.h>

#define RD_FILE_PATH_MAX 240u
#define RD_FILE_IDLE_TICKS 4000u
enum {
    RD_FILE_IDLE=0,RD_FILE_UPLOAD,RD_FILE_VERIFY_TEMP,RD_FILE_COMMITTING,
    RD_FILE_VERIFY_TARGET,RD_FILE_DONE,RD_FILE_ABORTING,RD_FILE_ABORTED,
    RD_FILE_FAILED,RD_FILE_RECOVERY,RD_FILE_READING,RD_FILE_LISTING
};
enum {
    RD_FILE_OK=0,RD_FILE_INVALID,RD_FILE_BUSY,RD_FILE_BINDING,RD_FILE_IO,
    RD_FILE_CRC,RD_FILE_OFFSET,RD_FILE_TOKEN,RD_FILE_OPEN,RD_FILE_NEEDS_RECOVERY,
    RD_FILE_NOT_FOUND,RD_FILE_CANCELLED,RD_FILE_TRANSPORT,RD_FILE_EXPIRED,
    RD_FILE_EXISTS,RD_FILE_NOT_DIRECTORY,RD_FILE_IS_DIRECTORY,
    RD_FILE_NOT_EMPTY,RD_FILE_CROSS_VOLUME
};
enum {
    RD_FILE_OLD_EXISTS=1u,RD_FILE_TEMP_PRESENT=2u,RD_FILE_BACKUP_PRESENT=4u,
    RD_FILE_TARGET_NEW=8u,RD_FILE_ABORT_REQUEST=16u,RD_FILE_RECOVERY_FLAG=32u
};

/* Task context, IRQs and the scheduler enabled. Commands 40..49 and 60..66 are described
 * in protocol.md. init verifies bindings, but never clears an active/recovery
 * transaction. poll does at most one 4096-byte verification read per call.
 * cancel only publishes intent; poll closes/rolls back before USB ownership
 * may be restored. busy includes pending cleanup, but excludes RECOVERY. */
int rd_files_init(void);
int rd_files_busy(void);
void rd_files_cancel(unsigned reason,uint32_t now);
void rd_files_poll(uint32_t now);
unsigned rd_files_command(unsigned op,const uint8_t *p,unsigned n,
                          uint8_t *out,unsigned *status);
#endif
