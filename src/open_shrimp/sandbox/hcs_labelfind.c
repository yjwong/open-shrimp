/* labelfind <label>: print the block device whose ext4 volume label matches.
 *
 * Ubuntu's busybox-static ships without the volumeid feature (no blkid, no
 * findfs, no mount-by-LABEL), so the initramfs resolves the rootfs VHDX by
 * reading the ext4 superblock itself: magic 0xEF53 at superblock offset
 * 0x38, 16-byte volume name at 0x78, superblock at device offset 1024.
 * Whole-disk filesystems only -- every VHDX the backend attaches carries a
 * bare filesystem, no partition table.  Statically linked like everything
 * else in the initramfs; built by scripts/build_hcs_initrd.sh.
 */
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

static int label_matches(const char *dev, const char *want)
{
	unsigned char sb[256];
	int fd = open(dev, O_RDONLY);
	if (fd < 0)
		return 0;
	ssize_t n = pread(fd, sb, sizeof(sb), 1024);
	close(fd);
	if (n != (ssize_t)sizeof(sb))
		return 0;
	if (sb[0x38] != 0x53 || sb[0x39] != 0xEF)
		return 0;
	char label[17];
	memcpy(label, sb + 0x78, 16);
	label[16] = '\0';
	return strcmp(label, want) == 0;
}

int main(int argc, char **argv)
{
	if (argc != 2) {
		fprintf(stderr, "usage: labelfind <ext4-label>\n");
		return 2;
	}
	char dev[16];
	const char *prefixes[] = { "/dev/sd", "/dev/vd" };
	for (unsigned p = 0; p < 2; p++) {
		for (char c = 'a'; c <= 'z'; c++) {
			snprintf(dev, sizeof(dev), "%s%c", prefixes[p], c);
			if (label_matches(dev, argv[1])) {
				puts(dev);
				return 0;
			}
		}
	}
	return 1;
}
