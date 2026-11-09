//
// Created by vip99 on 6/22/2026.
//

#ifndef FLEXITAC_16X32_GLOBAL_INCLUDE_H
#define FLEXITAC_16X32_GLOBAL_INCLUDE_H

#include "at32f415_conf.h"
#include "wk_system.h"
#include "wk_gpio.h"
#include "wk_adc.h"
// #include "wk_dma.h"
#include "delay.h"
#include "uart.h"

void app_init(void);
void app_loop(void);
void app_systick_isr(void);

#endif //FLEXITAC_16X32_GLOBAL_INCLUDE_H
