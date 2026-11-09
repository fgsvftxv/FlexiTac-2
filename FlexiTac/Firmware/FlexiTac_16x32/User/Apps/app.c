#include "global_include.h"
#include "74hc595.h"
#include "usb_app.h"
#include "usb_core.h"
#include "cdc_class.h"
#include <string.h>
#include "flexitac_packet.h"

volatile uint8_t timer_1ms = 0;
volatile uint16_t timer_10ms = 0;
volatile uint8_t timer_100ms = 0;

static uint16_t sample_data[FLEXITAC_SAMPLE_COUNT];
// static uint16_t adc1_value;

/* Dynamic threshold: historical min/max of per-frame spread across all frames since reset */
static uint16_t dyn_min = 0xFFFFu;
static uint16_t dyn_max = 0u;

extern otg_core_type otg_core_struct_fs1;
// [0]: local, [1]: from uart1, [2]: from uart2, [3] reserved
// does not support chained transmission (etc. uart1 rx contains multiple packets from other nodes)
static uint8_t usb_tx_buf[4 * sizeof(flexitac_packet_t)];
uint8_t curr_tx_buf_packet_idx = 0;

uint8_t is_packet_init = 0;
static uint8_t uart_rx_packet_parse_buf[sizeof(flexitac_packet_t)];

void adc_start(void);
uint16_t get_crc16(const uint8_t* data, const size_t len);

/* Required by the USB middleware stack */
void usb_delay_us(const uint32_t us) { wk_delay_us(us); }

/* -------------------------------------------------------------------------
 * set_mux_row - select one of 16 rows via CD74HC4067 address pins S0-S3.
 *
 * Pin mapping (at32f415_wk_config.h):
 *   row bit 0 -> SR_ADDR_0 (PA1) -> S0 of CD74HC4067
 *   row bit 1 -> SR_ADDR_1 (PA0) -> S1
 *   row bit 2 -> SR_ADDR_2 (PA6) -> S2
 *   row bit 3 -> SR_ADDR_3 (PA4) -> S3
 * ------------------------------------------------------------------------- */
static inline void set_mux_row(const uint8_t row)
{
    if (row & 0x01u) GPIOA->scr = SR_ADDR_0_PIN; else GPIOA->clr = SR_ADDR_0_PIN;
    if (row & 0x02u) GPIOA->scr = SR_ADDR_1_PIN; else GPIOA->clr = SR_ADDR_1_PIN;
    if (row & 0x04u) GPIOA->scr = SR_ADDR_2_PIN; else GPIOA->clr = SR_ADDR_2_PIN;
    if (row & 0x08u) GPIOA->scr = SR_ADDR_3_PIN; else GPIOA->clr = SR_ADDR_3_PIN;
}

/* -------------------------------------------------------------------------
 * read_adc - trigger one preempt (injected) conversion, poll PCCE flag,
 * read result from preempt channel 1 data register.
 *
 * ADC is configured with ch5 on preempt channel 1, software trigger,
 * offset = 0. ADC clock = PCLK2 / 4 = 72 / 4 = 18 MHz.
 * At 100 Hz (10 ms / 512 samples = 19.5 us budget per sample):
 *   ADC_SAMPLETIME_55_5: (55.5+12.5)/18 MHz = 3.78 us/sample -> 1.93 ms total
 * ------------------------------------------------------------------------- */
static inline uint16_t read_adc(void)
{
    adc_flag_clear(ADC1, ADC_PCCE_FLAG);
    adc_preempt_software_trigger_enable(ADC1, TRUE);
    while (!adc_flag_get(ADC1, ADC_PCCE_FLAG));
    return (uint16_t)(adc_preempt_conversion_data_get(ADC1, ADC_PREEMPT_CHANNEL_1) & 0x0FFFu);
    // adc_flag_clear(ADC1, ADC_CCE_FLAG);
    // adc_ordinary_software_trigger_enable(ADC1, TRUE);
    // while (!adc_flag_get(ADC1, ADC_CCE_FLAG));
    // return (uint16_t)(ADC1->odt_bit.odt & 0x0FFFu);
}

static inline uint16_t flexitac_le16(const uint8_t* data)
{
    return (uint16_t)data[0] | ((uint16_t)data[1] << 8);
}

static uint8_t is_valid_flexitac_packet(const uint8_t* packet_buf)
{
    const flexitac_packet_t* packet = (const flexitac_packet_t*)packet_buf;
    const uint16_t data_size = flexitac_le16(packet->data_size);
    const uint16_t rx_crc = flexitac_le16(packet->crc16);
    const uint16_t calc_crc = get_crc16(packet_buf, sizeof(flexitac_packet_t) - sizeof(packet->crc16));

    return packet->ext_header_0x54 == 0x54u
        && packet->ext_header_0xaa == 0xAAu
        && data_size == sizeof(packet->data)
        && packet->data_header_0xaa == 0xAAu
        && packet->data_header_0x55 == 0x55u
        && rx_crc == calc_crc;
}

static uint8_t pop_valid_flexitac_packet(kfifo_static_t* rx_fifo, uint8_t* dst_packet)
{
    uint8_t header[2];

    while(kfifo_used_s(rx_fifo) >= sizeof(header))
    {
        kfifo_peek_s(rx_fifo, (char*)header, sizeof(header));
        if(header[0] == 0x54u && header[1] == 0xAAu)
        {
            if(kfifo_used_s(rx_fifo) < sizeof(flexitac_packet_t))
            {
                return 0;
            }

            kfifo_peek_s(rx_fifo, (char*)uart_rx_packet_parse_buf, sizeof(uart_rx_packet_parse_buf));
            if(is_valid_flexitac_packet(uart_rx_packet_parse_buf))
            {
                memcpy(dst_packet, uart_rx_packet_parse_buf, sizeof(flexitac_packet_t));
                kfifo_wipe_n_s(rx_fifo, sizeof(flexitac_packet_t));
                return 1;
            }
        }

        kfifo_wipe_n_s(rx_fifo, 1);
    }

    return 0;
}

static uint8_t append_uart_rx_packets(kfifo_static_t* rx_fifo, uint8_t* dst_buffer, const uint8_t packet_capacity, uint8_t* packet_count)
{
    uint8_t appended = 0;

    while(*packet_count < packet_capacity)
    {
        uint8_t* dst_packet = dst_buffer + ((uint16_t)(*packet_count) * sizeof(flexitac_packet_t));
        if(!pop_valid_flexitac_packet(rx_fifo, dst_packet))
        {
            break;
        }

        (*packet_count)++;
        appended++;
    }

    return appended;
}

volatile uint8_t uart1_idle_flag = 0;
volatile uint8_t uart2_idle_flag = 0;
uint8_t uart1_rx_valid = 0;
uint8_t uart2_rx_valid = 0;

void uart_idle_cb(uart_struct_t* uart)
{
    // Rule: Transmit packet to both uart, receive & pack all incoming packets through USB
    if(uart == &uart1)
    {
        uart1_idle_flag = 1;
    }
    else if(uart == &uart2)
    {
        uart2_idle_flag = 1;
    }
}

void app_init(void)
{
    delay_init();

    wk_usb_app_init();

    hc595_init();

    uart_init(&uart1, USART1, DMA1_CHANNEL2, DMA1_CHANNEL1);
    uart_init(&uart2, USART2, DMA1_CHANNEL4, DMA1_CHANNEL3);

    tmr_output_enable(TMR3, TRUE);
    TMR3->cctrl_bit.c1en = 1;
    tmr_counter_enable(TMR3, TRUE);

    adc_start();
    // adc_ordinary_software_trigger_enable(ADC1, TRUE);

    /* Enable MUX: CD74HC4067 E# (SR_ENABLE, PB8) active low */
    GPIOB->clr = SR_ENABLE_PIN;
}

void app_loop(void)
{
    if(timer_1ms)
    {
        if(timer_10ms++ >= 9)
        {
            /* ------------------------------------------------------------
             * Sensor scan at 100 Hz (every 10 ms).
             *
             * For each of 16 rows, select the row via CD74HC4067,
             * then walk a single '1' through the 32-column shift register,
             * reading the ADC at each position.
             *
             * Data layout: sample_data[row * 32 + col], row-major, 512 * 2 bytes
             * LED:   peak raw value across all 512 samples -> 0-999 -> TMR3->c1dt.
             * ------------------------------------------------------------ */

            uint16_t frame_min = 0xFFFFu;
            uint16_t frame_max = 0u;

             for (uint8_t row = 0u; row < 16u; row++)
             {
                 set_mux_row(row);
                 // delay_us(1);
                 const uint16_t base = row * 32u;

                 // Column 0: load walking-1, latch, read
                 hc595_start_walk();
                 delay_us(1);
                 uint16_t raw = read_adc();
                 sample_data[base] = raw;
                 if (raw < frame_min) frame_min = raw;
                 if (raw > frame_max) frame_max = raw;

                 // Columns 1-31: advance one position, latch, read
                 for (uint8_t col = 1u; col < 32u; col++)
                 {
                     hc595_walk_next();
                     delay_us(1);
                     raw = read_adc();
                     sample_data[base + col] = raw;
                     if (raw < frame_min) frame_min = raw;
                     if (raw > frame_max) frame_max = raw;
                 }
             }


            /* Deactivate all columns */
            // hc595_clear();
            // GPIOB->scr = SR_ENABLE_PIN;

            uint16_t buf_size = sizeof(flexitac_packet_t);
            is_packet_init = build_flexitac_packet(usb_tx_buf, &buf_size, sample_data, FLEXITAC_SAMPLE_COUNT);
            // write usb_tx_buf & uart Tx
            if(is_packet_init)
            {
                uint8_t usb_packet_count = 1;
                uart1_rx_valid = 0;
                uart2_rx_valid = 0;
                curr_tx_buf_packet_idx = 0;
                if(uart1_idle_flag)
                {
                    uart1_rx_valid = append_uart_rx_packets(&uart1.rx_fifo, usb_tx_buf, 4, &usb_packet_count);
                    uart1_idle_flag = 0;
                }
                if(uart2_idle_flag)
                {
                    uart2_rx_valid = append_uart_rx_packets(&uart2.rx_fifo, usb_tx_buf, 4, &usb_packet_count);
                    uart2_idle_flag = 0;
                }
                curr_tx_buf_packet_idx = usb_packet_count - 1;
                // transmit usb first
                usb_vcp_send_data(&otg_core_struct_fs1.dev, usb_tx_buf, usb_packet_count * sizeof(flexitac_packet_t));
                // Tx
                /* Never enqueue a partial packet if the UART is backlogged. */
                if(kfifo_unused_s(&uart1.tx_fifo) >= sizeof(flexitac_packet_t))
                    kfifo_put_s(&uart1.tx_fifo, (char*)usb_tx_buf, sizeof(flexitac_packet_t));
                uart_start_tx(&uart1);
                if(kfifo_unused_s(&uart2.tx_fifo) >= sizeof(flexitac_packet_t))
                    kfifo_put_s(&uart2.tx_fifo, (char*)usb_tx_buf, sizeof(flexitac_packet_t));
                uart_start_tx(&uart2);
            }

            /* Per-frame spread: difference between max and min sample in this frame */
            const uint16_t frame_spread = frame_max - frame_min;

            /* Dynamic threshold: track historical min/max of per-frame spread */
            if (frame_spread > dyn_max) dyn_max = frame_spread;
            if (frame_spread < dyn_min) dyn_min = frame_spread;

            /* Map current spread into [0, 999] using dynamic range.
             * Guard against division by zero when dyn_max == dyn_min. */
            const uint16_t range = dyn_max - dyn_min;
            TMR3->c1dt = (range > 0u)
                ? (uint16_t)((uint32_t)(frame_spread - dyn_min) * 999u / range)
                : 0u;

            timer_10ms = 0;
        }

        timer_1ms = 0;
    }
}

void app_systick_isr(void)
{
    timer_1ms = 1;
    tick_ms++;
}

void adc_start(void)
{
    wk_adc1_init();
    // /* init dma1 channel1 */
    // wk_dma1_channel1_init();
    // /* config dma channel transfer parameter */
    // /* user need to modify define values DMAx_CHANNELy_XXX_BASE_ADDR
    //    and DMAx_CHANNELy_BUFFER_SIZE in at32xxx_wk_config.h */
    // wk_dma_channel_config(DMA1_CHANNEL1,
    //                       (uint32_t)&ADC1->odt,
    //                       (uint32_t)&adc1_value,
    //                       1);
    // dma_channel_enable(DMA1_CHANNEL1, TRUE);
}
