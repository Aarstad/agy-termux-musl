/* musl compatibility shim for glibc-linked binaries.
 * Supplies the handful of glibc-only symbols musl does not define. */
#define _GNU_SOURCE
#include <fcntl.h>
#include <unistd.h>
#include <stdlib.h>
#include <stdarg.h>
#include <sys/types.h>

/* glibc exports these cancellation-point syscalls under __-prefixed aliases. */
int  __open(const char *p, int fl, ...) {
    mode_t m = 0;
    if (fl & O_CREAT) { va_list ap; va_start(ap, fl); m = va_arg(ap, int); va_end(ap); }
    return open(p, fl, m);
}
int     __close(int fd)                          { return close(fd); }
ssize_t __read(int fd, void *b, size_t n)        { return read(fd, b, n); }
off_t   __lseek(int fd, off_t o, int w)          { return lseek(fd, o, w); }

/* Cleanup-handler bookkeeping for glibc's cancellation unwinder. musl uses a
 * different mechanism; these are only reached on pthread_cancel, which this
 * binary does not use. */
void __pthread_register_cancel(void *buf)   { (void)buf; }
void __pthread_unregister_cancel(void *buf) { (void)buf; }

/* glibc's out-of-line forms of the major()/minor()/makedev() macros. */
unsigned int  gnu_dev_major(unsigned long long d) { return (unsigned)((d >> 8) & 0xfff) | ((unsigned)(d >> 32) & ~0xfffu); }
unsigned int  gnu_dev_minor(unsigned long long d) { return (unsigned)(d & 0xff) | ((unsigned)(d >> 12) & ~0xffu); }
unsigned long long gnu_dev_makedev(unsigned int ma, unsigned int mi) {
    return ((unsigned long long)(mi & 0xff)) | (((unsigned long long)(ma & 0xfff)) << 8)
         | (((unsigned long long)(mi & ~0xffu)) << 12) | (((unsigned long long)(ma & ~0xfffu)) << 32);
}

/* Obsolete glibc allocator: page-aligned, size rounded up to a page. */
void *pvalloc(size_t n) {
    size_t ps = (size_t)sysconf(_SC_PAGESIZE);
    size_t sz = (n + ps - 1) & ~(ps - 1);
    void *p = NULL;
    if (posix_memalign(&p, ps, sz ? sz : ps) != 0) return NULL;
    return p;
}
