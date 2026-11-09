#pragma once

#include "global_include.h"
#include "kfifo_s.h"

typedef struct uart_struct_t
{
    uint32_t RX_DMA_GL_FLAG;
    uint32_t RX_DMA_FDT_FLAG;
    uint32_t RX_DMA_HDT_FLAG;
    uint32_t RX_DMA_DTERR_FLAG;
    uint32_t TX_DMA_GL_FLAG;
    uint32_t last_dma_rx_size;
    usart_type* huart;
    dma_channel_type* tx_dma;
    dma_channel_type* rx_dma;
    char rx_buffer[KFIFO_STATIC_BUF_SIZE];
    char tx_buffer[KFIFO_STATIC_BUF_SIZE];
    kfifo_static_t tx_fifo;
    kfifo_static_t rx_fifo;
} uart_struct_t;

void uart_init(uart_struct_t* uart, usart_type* huart, dma_channel_type* tx_dma, dma_channel_type* rx_dma);
void uart_on_idle_irq(uart_struct_t* uart);
void uart_on_rx_dma_irq(uart_struct_t* uart);
void uart_idle_cb(uart_struct_t* uart);
void uart_start_tx(uart_struct_t* uart);

// extern kfifo_static_t tx_fifo;
// extern kfifo_static_t rx_fifo;

extern uart_struct_t uart1;
extern uart_struct_t uart2;