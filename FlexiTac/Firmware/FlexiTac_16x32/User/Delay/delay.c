//
// Created by vip99 on 6/22/2026.
//
#include "delay.h"

void delay_init(void)
{
    /* Disable TRC */
    CoreDebug->DEMCR &= ~CoreDebug_DEMCR_TRCENA_Msk; // ~0x01000000;
    /* Enable TRC */
    CoreDebug->DEMCR |= CoreDebug_DEMCR_TRCENA_Msk; // 0x01000000;
    /* Disable Clock Cycle Counter */
    DWT->CTRL &= ~DWT_CTRL_CYCCNTENA_Msk; // ~0x00000001;
    /* Enable Clock Cycle Counter */
    DWT->CTRL |= DWT_CTRL_CYCCNTENA_Msk; // 0x00000001;
    /* Reset the Clock Cycle Counter value */
    DWT->CYCCNT = 0;
    /* 3 NOPs */
    __ASM volatile ("NOP");
    __ASM volatile ("NOP");
    __ASM volatile ("NOP");
    // LL Driver turns off SysTick_Handler() interrupt without SysTick_CTRL_TICKINT_Msk set.
    // SysTick->CTRL = SysTick_CTRL_CLKSOURCE_Msk |
    //                 SysTick_CTRL_TICKINT_Msk |
    //                 SysTick_CTRL_ENABLE_Msk;
}

void delay_cycle(const volatile uint32_t cycle)
{
    const uint32_t start_cycle = DWT->CYCCNT;
    while((DWT->CYCCNT - start_cycle) < cycle);
}

void delay_us(volatile uint32_t us)
{
    const uint32_t start_cycle = DWT->CYCCNT;
    us *= (SystemCoreClock / 1000000);
    while((DWT->CYCCNT - start_cycle) < us); // DWT->CYCCNT is a 32-bit register, and it takes account of overflow
}