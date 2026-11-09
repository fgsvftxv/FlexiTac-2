#include "kfifo_s.h"

#define uint32_min(x, y) ({       \
uint32_t _min1 = (x);         \
uint32_t _min2 = (y);         \
(void) (&_min1 == &_min2);    \
_min1 < _min2 ? _min1 : _min2;})

void kfifo_init_s(kfifo_static_t *fifo, uint32_t size)
{
    if(size & (size - 1)) size = roundup_pow2(size);
    fifo->size = size;
    fifo->in = 0;
    fifo->out = 0;
    memset(fifo->buffer, 0, size);
}
uint32_t kfifo_put_s(kfifo_static_t *fifo, char *buffer, uint32_t len)
{
    uint32_t l;
    len = uint32_min(len, (fifo->size - fifo->in + fifo->out));
    l = uint32_min(len, (fifo->size - (fifo->in & (fifo->size - 1))));
    memcpy(fifo->buffer + (fifo->in & (fifo->size - 1)), buffer, l);
    memcpy(fifo->buffer, buffer + l, len - l);
    fifo->in += len;
    return len;
}
uint32_t kfifo_peek_s(kfifo_static_t *fifo, char *buffer, uint32_t len)
{
    uint32_t l;
    len = uint32_min(len, (fifo->in - fifo->out));
    l = uint32_min(len, (fifo->size - (fifo->out & (fifo->size - 1))));
    memcpy(buffer, fifo->buffer + (fifo->out & (fifo->size - 1)), l);
    memcpy(buffer + l, fifo->buffer, len - l);
    return len;
}
uint32_t kfifo_get_s(kfifo_static_t *fifo, char *buffer, uint32_t len)
{
    len = kfifo_peek_s(fifo, buffer, len);
    fifo->out += len;
    return len;
}

void kfifo_wipe_n_s(kfifo_static_t *fifo, uint32_t len)
{
    fifo->out += len;
}