#include "74hc595.h"

/*
 * Pin mapping (defined in at32f415_wk_config.h):
 *   SER_IN → PA15  (serial data input to first 595)
 *   SRCLK  → PB1   (shift register clock, all 4 chips in parallel)
 *   RCLK   → PA10  (storage/latch clock, all 4 chips in parallel)
 *
 * 4 x SN74HC595 chained:  SER -> Chip1 -> Chip2 -> Chip3 -> Chip4
 *   Chip1 Q0-Q7  = column bits  0- 7
 *   Chip2 Q0-Q7  = column bits  8-15
 *   Chip3 Q0-Q7  = column bits 16-23
 *   Chip4 Q0-Q7  = column bits 24-31
 *
 * Shift order: MSB first (bit 31 shifted first -> arrives at Chip4 Q7 after 32 clocks).
 * Column col is activated by pattern (1u << col): bit 0 = column 0 (Chip1 Q0).
 */

/*
 * Atomic set / clear via AT32 dedicated registers (same as gpio_bits_set/reset):
 *   gpio_x->scr = pins  -> set pins HIGH  (Set Control Register)
 *   gpio_x->clr = pins  -> set pins LOW   (Clear Register)
 */
#define _SR_SET(port, pin)  ((port)->scr = (uint16_t)(pin))
#define _SR_CLR(port, pin)  ((port)->clr = (uint16_t)(pin))

#define SER_1()     _SR_SET(SER_IN_GPIO_PORT, SER_IN_PIN)
#define SER_0()     _SR_CLR(SER_IN_GPIO_PORT, SER_IN_PIN)
#define SRCLK_1()   _SR_SET(SRCLK_GPIO_PORT,  SRCLK_PIN)
#define SRCLK_0()   _SR_CLR(SRCLK_GPIO_PORT,  SRCLK_PIN)
#define RCLK_1()    _SR_SET(RCLK_GPIO_PORT,   RCLK_PIN)
#define RCLK_0()    _SR_CLR(RCLK_GPIO_PORT,   RCLK_PIN)

/* Half-period in CPU cycles, computed once from system_core_clock in hc595_init() */
static uint32_t s_half_cycles;

/* -------------------------------------------------------------------------
 * shift_byte — clock 8 bits into the shift register, MSB first.
 * SER is set up before the rising SRCLK edge (tsu satisfied by half_cycles delay).
 * ------------------------------------------------------------------------- */
static void shift_byte(uint8_t byte)
{
    for (int8_t i = 7; i >= 0; i--) {
        if (byte & (uint8_t)(1u << i)) { SER_1(); } else { SER_0(); }
        delay_cycle(s_half_cycles);   /* tw(SRCLK)L >= 30 ns at 3.3V */
        SRCLK_1();
        delay_cycle(s_half_cycles);   /* tw(SRCLK)H >= 30 ns at 3.3V */
        SRCLK_0();
    }
}

/* -------------------------------------------------------------------------
 * shift_32 — clock 32 bits into the 4-chip chain, MSB first.
 * bit 31 enters first -> propagates to Chip4 Q7; bit 0 stays at Chip1 Q0.
 * ------------------------------------------------------------------------- */
static void shift_32(uint32_t data)
{
    shift_byte((uint8_t)(data >> 24));   /* bits 31-24 -> Chip4 Q7-Q0 */
    shift_byte((uint8_t)(data >> 16));   /* bits 23-16 -> Chip3 Q7-Q0 */
    shift_byte((uint8_t)(data >>  8));   /* bits 15- 8 -> Chip2 Q7-Q0 */
    shift_byte((uint8_t)(data      ));   /* bits  7- 0 -> Chip1 Q7-Q0 */
}

/* Latch shift register -> output register with a single RCLK pulse */
static void latch(void)
{
    RCLK_1();
    delay_cycle(s_half_cycles);
    RCLK_0();
}

/* =========================================================================
 * Public API
 * ========================================================================= */

void hc595_init(void)
{
    /*
     * Half-period = system_core_clock / (2 x target_freq)
     * At 144 MHz: 144000000 / 16000000 = 9 cycles ~63 ns  -> SRCLK ~8 MHz
     * At  72 MHz:  72000000 / 16000000 = 4 cycles ~56 ns  -> SRCLK ~9 MHz
     */
    s_half_cycles = system_core_clock / (2u * HC595_SRCLK_FREQ_HZ);
    if (s_half_cycles < 1u) { s_half_cycles = 1u; }

    SRCLK_0();
    RCLK_0();
    SER_0();

    shift_32(0x00000000u);
    latch();
}

/* Turn off all column outputs */
void hc595_clear(void)
{
    shift_32(0x00000000u);
    latch();
}

/*
 * hc595_set_column - activate exactly one column (full 32-bit pattern).
 * Column 0 -> Chip1 Q0, Column 31 -> Chip4 Q7.
 * col must be in [0, HC595_COLS-1].
 */
void hc595_set_column(uint8_t col)
{
    shift_32((uint32_t)1u << col);
    latch();
}

/* -------------------------------------------------------------------------
 * Walking-1 API
 *
 * hc595_start_walk: load a single '1' into bit position 0 (Chip1 Q0) and
 *   latch -> column 0 is selected. SER is left LOW for subsequent walk_next calls.
 *
 * hc595_walk_next: clock one '0' into the register (SER already LOW), shifting
 *   the existing '1' one position forward, then latch -> next column selected.
 *
 * Timing per call (at 8 MHz SRCLK, half_cycles = 9 @ 144 MHz):
 *   start_walk : 1 bit shift + 1 RCLK pulse ~= 3 x 63 ns = ~190 ns
 *   walk_next  : 1 bit shift + 1 RCLK pulse ~= 3 x 63 ns = ~190 ns
 * ------------------------------------------------------------------------- */
void hc595_start_walk(void)
{
    /* Shift in '1' at position 0 */
    SER_1();
    delay_cycle(s_half_cycles);   /* setup time before rising SRCLK */
    SRCLK_1();
    delay_cycle(s_half_cycles);
    SRCLK_0();
    SER_0();                       /* hold satisfied; clear SER for walk_next */

    latch();                       /* Q0 of Chip1 = 1, all others = 0 */
}

void hc595_walk_next(void)
{
    /* SER is already 0 from start_walk or previous walk_next.
     * Clock one '0' in: the existing '1' advances one position. */
    delay_cycle(s_half_cycles);   /* tw(SRCLK)L */
    SRCLK_1();
    delay_cycle(s_half_cycles);   /* tw(SRCLK)H */
    SRCLK_0();

    latch();
}
