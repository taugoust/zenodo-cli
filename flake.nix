{
  description = "Small verified Zenodo draft uploader";
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  outputs = { self, nixpkgs }:
    let
      systems = [ "aarch64-darwin" "x86_64-darwin" "aarch64-linux" "x86_64-linux" ];
      each = nixpkgs.lib.genAttrs systems;
      environment = system: let pkgs = nixpkgs.legacyPackages.${system};
        in pkgs.python3.withPackages (ps: [ ps.requests ps.tqdm ]);
    in {
      packages = each (system: let pkgs = nixpkgs.legacyPackages.${system}; in {
        default = pkgs.writeShellApplication {
          name = "zenodo-upload";
          text = ''exec ${environment system}/bin/python ${./zenodo_upload.py} "$@"'';
        };
      });
      devShells = each (system: {
        default = nixpkgs.legacyPackages.${system}.mkShell {
          packages = [ (environment system) ];
        };
      });
      checks = each (system: {
        mocks = nixpkgs.legacyPackages.${system}.runCommand "zenodo-uploader-mocks" {
          nativeBuildInputs = [ (environment system) ];
        } ''
          cp ${./zenodo_upload.py} zenodo_upload.py
          cp ${./test_uploader.py} test_uploader.py
          python -m unittest -v test_uploader
          touch "$out"
        '';
      });
    };
}
