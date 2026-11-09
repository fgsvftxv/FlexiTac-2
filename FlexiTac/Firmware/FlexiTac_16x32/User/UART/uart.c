#include "uart.h"

uart_struct_t uart1;
uart_struct_t uart2;

uint8_t get_dma_index_by_chaddr(uint32_t addr)
{
    if(addr > DMA2_CHANNEL7_BASE) return 3;
    if(addr >= DMA2_BASE) return 2;
    if(addr <= DMA1_CHANNEL7_BASE && addr >= DMA1_BASE) return 1;
    return 0;
}

uint8_t get_channel_index_by_chaddr(uint32_t addr)
{
    const uint8_t dma_index = get_dma_index_by_chaddr(addr);
    if(dma_index == 1)
    {
        const uint32_t offset = DMA1_CHANNEL2_BASE - DMA1_CHANNEL1_BASE;
        return ((addr - DMA1_CHANNEL1_BASE) / offset) + 1;
    }
    if(dma_index == 2)
    {
        const uint32_t offset = DMA2_CHANNEL2_BASE - DMA2_CHANNEL1_BASE;
        return ((addr - DMA2_CHANNEL1_BASE) / offset) + 1;
    }
    return 0;
}

void uart_init(uart_struct_t* uart, usart_type* huart, dma_channel_type* tx_dma, dma_channel_type* rx_dma)
{
    uart->huart = huart;
    uart->tx_dma = tx_dma;
    uart->rx_dma = rx_dma;
    usart_reset(uart->huart);
    // FuncRetCode ret = tx_fifo.init(TX_FIFO_BUFFER_SIZE);
    // if(ret != FuncRetCode::OK) return ret;
    // ret = rx_fifo.init(RX_FIFO_BUFFER_SIZE);
    // if(ret != FuncRetCode::OK) return ret;
    kfifo_init_s(&uart->tx_fifo, KFIFO_STATIC_BUF_SIZE);
    kfifo_init_s(&uart->rx_fifo, KFIFO_STATIC_BUF_SIZE);
    /* ADC12: 1038 bytes/frame at 100 Hz needs >1 Mbit/s (8N1).
     * All directly connected boards must use this baud and packet format. */
    uint32_t baudrate = 2000000;
    // switch(baud)
    // {
    //     case DataType::Comm::UARTBaudrate::BAUD_9600: baudrate = 9600; break;
    //     case DataType::Comm::UARTBaudrate::BAUD_230400: baudrate = 230400; break;
    //     case DataType::Comm::UARTBaudrate::BAUD_460800: baudrate = 460800; break;
    //     case DataType::Comm::UARTBaudrate::BAUD_921600: baudrate = 921600; delay = 2; break;
    //     case DataType::Comm::UARTBaudrate::BAUD_1843200: baudrate = 1843200; delay = 2; break;
    //     case DataType::Comm::UARTBaudrate::BAUD_1000000: baudrate = 1000000; delay = 2; break;
    //     case DataType::Comm::UARTBaudrate::BAUD_4000000: baudrate = 4000000; delay = 8; break;
    //     case DataType::Comm::UARTBaudrate::BAUD_6000000: baudrate = 6000000; delay = 12; break;
    //     case DataType::Comm::UARTBaudrate::BAUD_8000000: baudrate = 8000000; delay = 18; break;
    //     case DataType::Comm::UARTBaudrate::BAUD_9000000: baudrate = 9000000; delay = 22; break;
    //     default: break;
    // }
    dma_channel_enable(rx_dma, FALSE);
    dma_channel_enable(tx_dma, FALSE);
    // RX DMA Settings
    const uint8_t RX_DMA_INDEX = get_dma_index_by_chaddr((uint32_t)rx_dma);
    const uint8_t RX_DMA_CHANNEL = get_channel_index_by_chaddr((uint32_t)rx_dma);
    if(RX_DMA_INDEX > 2 || RX_DMA_INDEX < 1 || RX_DMA_CHANNEL > 7 || RX_DMA_CHANNEL < 1) return;
    uart->RX_DMA_GL_FLAG    = ((RX_DMA_INDEX - 1) << 28) | (1 << (4 * (RX_DMA_CHANNEL - 1)));
    uart->RX_DMA_DTERR_FLAG = ((RX_DMA_INDEX - 1) << 28) | (8 << (4 * (RX_DMA_CHANNEL - 1)));
    uart->RX_DMA_FDT_FLAG = ((RX_DMA_INDEX - 1) << 28) | (2 << (4 * (RX_DMA_CHANNEL - 1)));
    uart->RX_DMA_HDT_FLAG = ((RX_DMA_INDEX - 1) << 28) | (4 << (4 * (RX_DMA_CHANNEL - 1)));
    rx_dma->ctrl_bit.mincm  = TRUE;
    rx_dma->ctrl_bit.pincm  = FALSE;
    rx_dma->ctrl_bit.pwidth = DMA_PERIPHERAL_DATA_WIDTH_BYTE;
    rx_dma->ctrl_bit.mwidth = DMA_MEMORY_DATA_WIDTH_BYTE;
    rx_dma->ctrl_bit.lm     = TRUE; // Circular mode
    rx_dma->dtcnt = sizeof(uart->rx_buffer);
    rx_dma->paddr = (uint32_t)&huart->dt;
    rx_dma->maddr = (uint32_t)uart->rx_buffer;
    // TX DMA Settings
    const uint8_t TX_DMA_INDEX = get_dma_index_by_chaddr((uint32_t)tx_dma);
    const uint8_t TX_DMA_CHANNEL = get_channel_index_by_chaddr((uint32_t)tx_dma);
    if(TX_DMA_INDEX > 2 || TX_DMA_INDEX < 1 || TX_DMA_CHANNEL > 7 || TX_DMA_CHANNEL < 1) return;
    uart->TX_DMA_GL_FLAG = ((TX_DMA_INDEX - 1) << 28) | (1 << (4 * (TX_DMA_CHANNEL - 1)));
    tx_dma->ctrl_bit.mincm  = TRUE;
    tx_dma->ctrl_bit.pincm  = FALSE;
    tx_dma->ctrl_bit.pwidth = DMA_PERIPHERAL_DATA_WIDTH_BYTE;
    tx_dma->ctrl_bit.mwidth = DMA_MEMORY_DATA_WIDTH_BYTE;
    tx_dma->ctrl_bit.lm     = FALSE;
    tx_dma->dtcnt = 0;
    tx_dma->paddr = (uint32_t)&huart->dt;
    // tx_buffer.fill(0);
    // rx_buffer.fill(0);
    memset(uart->tx_buffer, 0, KFIFO_STATIC_BUF_SIZE);
    memset(uart->rx_buffer, 0, KFIFO_STATIC_BUF_SIZE);
    // UART Init
    usart_init(huart, baudrate, USART_DATA_8BITS, USART_STOP_1_BIT);

    dma_channel_enable(rx_dma, TRUE);
    usart_receiver_enable(huart, TRUE);
    usart_dma_receiver_enable(huart, TRUE);
    usart_transmitter_enable(huart, TRUE);
    usart_dma_transmitter_enable(huart, TRUE);
    usart_interrupt_enable(huart, USART_IDLE_INT, TRUE);
    dma_interrupt_enable(rx_dma, DMA_FDT_INT | DMA_HDT_INT, TRUE);
    usart_enable(huart, TRUE);
}

void uart_on_idle_irq(uart_struct_t* uart)
{
    if(uart->huart->sts & USART_IDLEF_FLAG)
    {
        const uint16_t recv_total_size = sizeof(uart->rx_buffer) - uart->rx_dma->dtcnt;
        if(recv_total_size >= uart->last_dma_rx_size)
        {
            const uint16_t recv_size = recv_total_size - uart->last_dma_rx_size;
            // if(recv_size > 0) rx_fifo.put(rx_buffer.data() + last_dma_rx_size, recv_size);
            if(recv_size > 0) kfifo_put_s(&uart->rx_fifo, uart->rx_buffer + uart->last_dma_rx_size, recv_size);
        }
        else // buffer rollback
        {
            const uint16_t tail = sizeof(uart->rx_buffer) - uart->last_dma_rx_size;
            // rx_fifo.put(rx_buffer.data() + last_dma_rx_size, tail);
            // if(recv_total_size > 0) rx_fifo.put(rx_buffer.data(), recv_total_size);
            kfifo_put_s(&uart->rx_fifo, uart->rx_buffer + uart->last_dma_rx_size, tail);
            if(recv_total_size > 0) kfifo_put_s(&uart->rx_fifo, uart->rx_buffer, recv_total_size);
        }
        uart->last_dma_rx_size = recv_total_size;
        // vTaskNotifyGiveFromISR(event_handler.GetHandle(), nullptr);
        // usart_flag_clear(huart, USART_IDLEF_FLAG);
        uart_idle_cb(uart);
        UNUSED(uart->huart->sts);
        UNUSED(uart->huart->dt);
    }
}

void uart_start_tx(uart_struct_t* uart)
{
    if(uart->tx_dma->ctrl_bit.chen == 0 || uart->tx_dma->dtcnt == 0)
    {
        const uint16_t len = kfifo_get_s(&uart->tx_fifo, uart->tx_buffer, sizeof(uart->tx_buffer));
        if(len > 0)
        {
            dma_channel_enable(uart->tx_dma, FALSE);
            uart->tx_dma->dtcnt = len;
            uart->tx_dma->maddr = (uint32_t)uart->tx_buffer;
            dma_flag_clear(uart->TX_DMA_GL_FLAG);
            dma_channel_enable(uart->tx_dma, TRUE);
        }
    }
}

void uart_on_rx_dma_irq(uart_struct_t* uart)
{
    if(dma_interrupt_flag_get(uart->RX_DMA_DTERR_FLAG) != RESET)
    {
        dma_flag_clear(uart->RX_DMA_DTERR_FLAG);
    }
    if(dma_interrupt_flag_get(uart->RX_DMA_FDT_FLAG) != RESET) // DMA Full Complete, ~HAL_UART_RXEVENT_TC
    {
        const uint16_t recv_size = sizeof(uart->rx_buffer) - uart->last_dma_rx_size;
        // rx_fifo.put(rx_buffer.data() + last_dma_rx_size, recv_size);
        kfifo_put_s(&uart->rx_fifo, uart->rx_buffer + uart->last_dma_rx_size, recv_size);
        uart->last_dma_rx_size = 0;
        dma_flag_clear(uart->RX_DMA_FDT_FLAG);
    }
    if(dma_interrupt_flag_get(uart->RX_DMA_HDT_FLAG) != RESET) // DMA Half Complete, ~HAL_UART_RXEVENT_HT
    {
        const uint16_t recv_total_size = sizeof(uart->rx_buffer) - uart->rx_dma->dtcnt;
        const uint16_t recv_size = recv_total_size - uart->last_dma_rx_size;
        // rx_fifo.put(rx_buffer.data() + last_dma_rx_size, recv_size);
        kfifo_put_s(&uart->rx_fifo, uart->rx_buffer + uart->last_dma_rx_size, recv_size);
        uart->last_dma_rx_size = recv_total_size;
        dma_flag_clear(uart->RX_DMA_HDT_FLAG);
    }
    if(dma_interrupt_flag_get(uart->RX_DMA_GL_FLAG)) dma_flag_clear(uart->RX_DMA_GL_FLAG);
}
