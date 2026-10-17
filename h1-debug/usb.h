#ifndef H1_USB_DEBUG_H
#define H1_USB_DEBUG_H
#include <stdint.h>
/* Full-speed CDC ACM on JZ4740; application owns controller while running. */
typedef struct {
    uint32_t clock_gate,opcr,imr,pdmask,pdflag;
    uint16_t txe,rxe,maxp[3][2],csr[3][2];
    uint8_t power,index,usbe,address,owned,configured,control_phase,reset_pending;
    uint8_t control[128],line[7],pending_address;
    uint16_t control_length,control_offset;
    uint32_t resets,setups,stalls,rx_packets,tx_packets,ownership_fault;
    uint32_t fault_reason,fault_imr,fault_pdmask,fault_pin;
    uint32_t reset_tick;
    uint16_t fault_txe,fault_rxe;
    uint8_t fault_usbe,fault_events,fault_power;
    uint32_t session_epoch;
    uint16_t control_lines;
} debug_usb;
int debug_usb_open(debug_usb *u);
void debug_usb_poll(debug_usb *u);
int debug_usb_read(debug_usb *u,uint8_t *data,unsigned capacity);
int debug_usb_write(debug_usb *u,const uint8_t *data,unsigned length);
int debug_usb_tx_idle(debug_usb *u);
int debug_usb_cable_connected(void);
/* Read-only last received full-speed USB frame number (11 bits). */
unsigned debug_usb_frame(void);
void debug_usb_close(debug_usb *u);
#endif
