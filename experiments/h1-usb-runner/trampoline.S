    .set noreorder
    .text
    .globl h1_exec_trampoline
    .type h1_exec_trampoline,@function
/* Save host O32 callee state on host stack, run with a separate module stack.
 * The saved SP anchor is outside module-owned RAM. This is fault detection
 * and ABI recovery for trusted code, never memory protection or a sandbox.
 */
h1_exec_trampoline:
    addiu $sp,$sp,-64
    sw $s0,0($sp)
    sw $s1,4($sp)
    sw $s2,8($sp)
    sw $s3,12($sp)
    sw $s4,16($sp)
    sw $s5,20($sp)
    sw $s6,24($sp)
    sw $s7,28($sp)
    sw $fp,32($sp)
    sw $gp,36($sp)
    sw $ra,40($sp)
    mfc0 $t0,$12
    nop
    sw $t0,44($sp)
    lui $t0,%hi(h1_exec_saved_sp)
    sw $sp,%lo(h1_exec_saved_sp)($t0)
    move $t9,$a0
    move $a0,$a1
    move $a1,$a2
    addiu $sp,$a3,-16
    jalr $t9
    nop
    lui $t0,%hi(h1_exec_saved_sp)
    lw $sp,%lo(h1_exec_saved_sp)($t0)
    lw $t0,44($sp)
    mtc0 $t0,$12
    nop
    nop
    nop
    lw $s0,0($sp)
    lw $s1,4($sp)
    lw $s2,8($sp)
    lw $s3,12($sp)
    lw $s4,16($sp)
    lw $s5,20($sp)
    lw $s6,24($sp)
    lw $s7,28($sp)
    lw $fp,32($sp)
    lw $gp,36($sp)
    lw $ra,40($sp)
    jr $ra
    addiu $sp,$sp,64
    .size h1_exec_trampoline,.-h1_exec_trampoline
