/* agentinbox-runner: the only binary that holds TCC grants.
 *
 * It forks and execs the fixed agentinbox entry point (path baked in at
 * compile time) and nothing else. launchd runs it as the job executable, so
 * it is the TCC "responsible process": the python child inherits its Full
 * Disk Access. Run directly by anything else, responsibility falls to that
 * caller's host app, which has no grants - so this binary cannot be used to
 * read protected data outside the launchd jobs. Python itself holds no TCC
 * grants at all.
 *
 * fork+wait (not plain exec): exec would replace this process's code
 * identity with the python interpreter's, and the grant would no longer
 * match. The runner must stay alive as the granted process.
 */
#include <stdio.h>
#include <sys/wait.h>
#include <unistd.h>

#ifndef AGENTINBOX_BIN
#error "compile with -DAGENTINBOX_BIN=\"/path/to/agentinbox\""
#endif

int main(int argc, char *argv[]) {
    char *args[argc + 1];
    args[0] = AGENTINBOX_BIN;
    for (int i = 1; i < argc; i++)
        args[i] = argv[i];
    args[argc] = NULL;

    pid_t pid = fork();
    if (pid < 0) {
        perror("agentinbox-runner: fork");
        return 126;
    }
    if (pid == 0) {
        execv(AGENTINBOX_BIN, args);
        perror("agentinbox-runner: execv " AGENTINBOX_BIN);
        _exit(127);
    }
    int status;
    if (waitpid(pid, &status, 0) < 0) {
        perror("agentinbox-runner: waitpid");
        return 126;
    }
    if (WIFEXITED(status))
        return WEXITSTATUS(status);
    if (WIFSIGNALED(status))
        return 128 + WTERMSIG(status);
    return 126;
}
