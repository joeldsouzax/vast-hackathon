{ pkgs ? import <nixpkgs> {} }:

pkgs.mkShell {
  nativeBuildInputs = with pkgs; [
    ffmpeg
    buildkit
    docker-buildx
  ];

  shellHook = ''
    export DOCKER_CONFIG="''${XDG_RUNTIME_DIR:-/tmp}/breadcast-docker-config"
    mkdir -p "$DOCKER_CONFIG/cli-plugins"
    ln -sfn ${pkgs.docker-buildx}/bin/docker-buildx "$DOCKER_CONFIG/cli-plugins/docker-buildx"
  '';
}
