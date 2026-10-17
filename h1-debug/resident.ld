ENTRY(rd_control)
SECTIONS {
  . = 0x81000000;
  .resident_header : { KEEP(*(.resident_header)) }
  .text : { *(.text*) }
  .rodata : { *(.rodata*) }
  .data : { *(.data*) *(.sdata*) }
  .got : { *(.got*) }
  . = ALIGN(32);
  __image_end = .;
  .bss (NOLOAD) : { __bss_start = .; *(.bss*) *(.sbss*) *(COMMON) . = ALIGN(32); __bss_end = .; }
  ASSERT(rd_control == 0x81000000, "resident control must lead image")
  ASSERT(__bss_end < 0x81200000, "resident allocation exceeds 2 MiB")
  /DISCARD/ : { *(.comment*) *(.note*) *(.MIPS.abiflags*) *(.reginfo*) *(.eh_frame*) *(.pdr*) }
}
