Placeholder so pack/Dockerfile.acr's COPY works for local builds without a UI
build. The pack-acr-overlay workflow replaces this directory with the built
gpustack-ui dist (index.html present -> overlaid onto <package>/ui/).
