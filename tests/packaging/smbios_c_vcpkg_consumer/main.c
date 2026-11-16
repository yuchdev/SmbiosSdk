/* Clean-room consumer of the vcpkg-installed smbios_c: opens the host's SMBIOS
 * table through the C ABI and prints only counts (no firmware values). A host
 * without a readable table (an unprivileged Linux runner) is a pass: smbios_open()
 * reports SMBIOS_ERR_NO_TABLE_FOUND instead of throwing, which is the documented
 * contract. Any other failure is a failure. */
#include <smbios/smbios.h>

#include <stdio.h>

int main(void)
{
    smbios_handle_t handle = NULL;
    smbios_error_t err = smbios_open(&handle);
    if (err == SMBIOS_ERR_NO_TABLE_FOUND) {
        printf("smbios_open=no-table\n");
        return 0;
    }
    if (err != SMBIOS_OK) {
        fprintf(stderr, "smbios_open failed: %d\n", (int)err);
        return 1;
    }

    size_t count = 0;
    err = smbios_entry_count(handle, &count);
    smbios_close(handle);
    if (err != SMBIOS_OK) {
        fprintf(stderr, "smbios_entry_count failed: %d\n", (int)err);
        return 1;
    }
    printf("smbios_open=ok structures=%zu\n", count);
    return 0;
}
