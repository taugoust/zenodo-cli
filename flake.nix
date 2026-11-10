{
  description = "Small verified Zenodo draft uploader";
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  outputs = { self, nixpkgs }:
    let
      systems = [ "aarch64-darwin" "x86_64-darwin" "aarch64-linux" "x86_64-linux" ];
      each = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
      project = (builtins.fromTOML (builtins.readFile ./pyproject.toml)).project;
      package = pkgs: pkgs.python3Packages.buildPythonApplication {
        pname = project.name;
        inherit (project) version;
        pyproject = true;
        src = nixpkgs.lib.fileset.toSource {
          root = ./.;
          fileset = nixpkgs.lib.fileset.unions [ ./pyproject.toml ./README.md ./LICENSE ./src ./tests ];
        };
        build-system = [ pkgs.python3Packages.setuptools ];
        dependencies = with pkgs.python3Packages; [ requests tqdm ];
        nativeCheckInputs = [ pkgs.python3Packages.unittestCheckHook ];
        unittestFlagsArray = [ "-s" "tests" "-v" "-b" ];
        pythonImportsCheck = [ "zenodo_upload" ];
        meta.mainProgram = "zenodo-upload";
      };
    in {
      packages = each (pkgs: { default = package pkgs; });
      devShells = each (pkgs: {
        default = pkgs.mkShell {
          packages = [ (pkgs.python3.withPackages (ps: [ ps.requests ps.tqdm ])) ];
          shellHook = ''export PYTHONPATH="$PWD/src''${PYTHONPATH:+:$PYTHONPATH}"'';
        };
      });
      checks = each (pkgs: { tests = package pkgs; });
    };
}
