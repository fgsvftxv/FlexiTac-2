//
// Created by vip99 on 6/22/2026.
//

#ifndef FLEXITAC_16X32_74HC595_H
#define FLEXITAC_16X32_74HC595_H

#include "global_include.h"

/*
 * SN74HC595PWR - AC Characteristics (TI datasheet Rev.J)
 *   VCC = 4.5V: tw(SRCLK)H/L = 20 ns min, fmax = 25 MHz
 *   VCC = 3.3V: tw(SRCLK)H/L ~30 ns min,  fmax ~16 MHz  (interpolated)
 *
 * HC595_SRCLK_FREQ_HZ targets 8 MHz - a 2x safety margin below the 3.3V limit.
 */
#define HC595_SRCLK_FREQ_HZ     4000000u

/* Number of columns driven by the 4 chained SN74HC595 (4 x 8 = 32 bits) */
#define HC595_COLS              32u

void hc595_init(void);
void hc595_set_column(uint8_t col);
void hc595_clear(void);

/*
 * Walking-1 scan API (mirrors original FlexiTac shiftColumn logic).
 * Faster than hc595_set_column: shifts only 1 bit per step instead of 32.
 *
 * Usage:
 *   hc595_start_walk();          // column 0 selected, latched
 *   read_adc();                  // sample column 0
 *   for col = 1..31:
 *     hc595_walk_next();         // advance to column col, latched
 *     read_adc();                // sample column col
 */
void hc595_start_walk(void);
void hc595_walk_next(void);

#endif //FLEXITAC_16X32_74HC595_H
