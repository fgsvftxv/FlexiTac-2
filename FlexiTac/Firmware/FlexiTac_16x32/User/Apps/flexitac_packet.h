#ifndef FLEXITAC_16X32_FLEXITAC_PACKET_H
#define FLEXITAC_16X32_FLEXITAC_PACKET_H

#include "stdint.h"

#define FLEXITAC_SAMPLE_COUNT 512u
#define FLEXITAC_DATA_BYTES (FLEXITAC_SAMPLE_COUNT * 2u)

typedef struct flexitac_packet_t
{
    uint8_t ext_header_0x54; // for Flexitac Mini use
    uint8_t ext_header_0xaa;
    uint8_t node_uuid_u16[2];
    uint8_t node_timestamp[4];
    uint8_t data_size[2];
    uint8_t data_header_0xaa; // framing marker; data_size distinguishes sample formats
    uint8_t data_header_0x55;
    uint8_t data[FLEXITAC_DATA_BYTES]; // 512 full ADC counts, uint16 little-endian
    uint8_t crc16[2];
} flexitac_packet_t;

_Static_assert(sizeof(flexitac_packet_t) == 1038u, "Unexpected ADC12 packet layout");

extern uint32_t tick_ms;
uint8_t build_flexitac_packet(uint8_t* dst_buffer, uint16_t* dst_buffer_in_out_size, const uint16_t* sample_data, uint16_t sample_count);

#endif //FLEXITAC_16X32_FLEXITAC_PACKET_H
