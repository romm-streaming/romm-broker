#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <file/keychain.h>
static void seal(const char *id, const char *v) {
   char *s = keychain_seal_alloc(id, v);
   printf("%s = \"%s\"\n", id, s ? s : "(null)");
   free(s);
}
int main(int argc, char **argv) {
   /* argv[1] key file, argv[2] "-", "set:<pass>" or "unlock:<pass>" , argv[3] "seal"|"open" , argv[4] name and argv[5] value to open */
   if (!keychain_init(argv[1]) && !keychain_is_locked()) { printf("init failed\n"); return 1; }
   if (keychain_is_locked() && strncmp(argv[2], "unlock:", 7)) { printf("locked\n"); return 1; }
   if (!strncmp(argv[2], "set:", 4) && !keychain_set_passphrase(argv[2] + 4)) { printf("passphrase failed\n"); return 1; }
   if (!strncmp(argv[2], "unlock:", 7) && !keychain_unlock(argv[2] + 7)) { printf("unlock failed\n"); return 1; }
   if (!strcmp(argv[3], "open")) { char *p = keychain_open_alloc(argv[4], argv[5]); printf("%s\n", p ? p : "(null)"); return 0; }
   seal("cheevos_username", "alice");
   seal("cheevos_token", "tok123");
   seal("cheevos_password", "");
   return 0;
}
