# Docker Container

A Docker container is a runtime instance of a Docker image — an isolated
process that shares the host kernel but runs with its own filesystem,
network interface, and process namespace. Containers package an
application together with its dependencies so the same artifact runs
identically across environments.

## Configuring a Container

The most common way to launch a container is `docker run`:

```bash
docker run -d --name web -p 8080:80 \
  -e ENV=production \
  -v $(pwd)/data:/var/data \
  --restart unless-stopped \
  nginx:1.27
```

Key flags:

- `-d` detaches the container as a background process.
- `-p host:container` publishes a port from the container to the host.
- `-v src:dst` bind-mounts a host path or named volume into the container.
- `--restart` selects the restart policy on exit or daemon restart.
- `-e KEY=VAL` sets environment variables visible inside the container.

## Best Practices

- Pin image tags to a specific digest; rebuild on dependency updates.
- Run as a non-root user (`USER` directive in the Dockerfile).
- Keep the container single-process so signals and exit codes propagate.
- Externalize state: use volumes for data, environment variables for config.

## Troubleshooting

- `docker ps -a` shows stopped containers and their exit codes.
- `docker logs <name>` streams container stdout/stderr.
- `docker exec -it <name> sh` opens a shell inside a running container.
- An OOM kill appears as exit code 137; tune memory limits with `--memory`.
