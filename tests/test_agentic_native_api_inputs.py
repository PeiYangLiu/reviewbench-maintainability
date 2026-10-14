"""Literal six-plan operational fixtures; source paths alone are relocated."""

ORIGINAL_PLANS = [{'repo': 'AutoPas/AutoPas',
  'input_index': 1,
  'configure_command': None,
  'build_command': 'set -e; rm -rf oracle-selector oracle-selector-cmake; mkdir oracle-selector-cmake; cp '
                   "'{root}/harnesses/CMakeLists.autopas1.txt' oracle-selector-cmake/CMakeLists.txt; "
                   'variant_flag=\'\'; if test "$ORACLE_VARIANT" = golden; then '
                   'variant_flag=-DAUTOPAS_ORACLE_METHOD_INTERACTION=ON; fi; cmake -S oracle-selector-cmake '
                   '-B oracle-selector -GNinja -DCMAKE_BUILD_TYPE=Debug -DCMAKE_CXX_COMPILER=g++-11 '
                   '-DAUTOPAS_REPOSITORY=$PWD '
                   "-DAUTOPAS_ORACLE_HARNESS='{root}/harnesses/autopas_traversal_selector.cpp' "
                   '$variant_flag; cmake --build oracle-selector --target autopas_traversal_oracle -j2',
  'test_command': 'set -e; out=$(./oracle-selector/autopas_traversal_oracle); printf \'%s\\n\' "$out"; test '
                  '"$out" = \'1 1 1 1\'',
  'real_translation_units': ['src/autopas/tuning/selectors/TraversalSelector.h',
                             'src/autopas/utils/ExceptionHandler.cpp',
                             'applicationLibrary/molecularDynamics/molecularDynamicsLibrary/MoleculeLJ.cpp'],
  'docker_image': 'reviewbench-hard-native-autopas:20260804',
  'timeout_seconds': 1800,
  'build_jobs': 2},
 {'repo': 'AutoPas/AutoPas',
  'input_index': 2,
  'configure_command': None,
  'build_command': "set -e; rm -rf oracle-ppl; mkdir oracle-ppl; variant_flag=''; if test "
                   '"$ORACLE_VARIANT" = golden; then variant_flag=-DAUTOPAS_ORACLE_SEPARATE_SITE_PARAMETERS; '
                   'fi; g++-11 -std=c++17 -O0 -g --coverage -IapplicationLibrary/molecularDynamics -Isrc '
                   '-I{root}/cache/spdlog-e86be93b4a193e78a78ff894b7b3d454307241bb/include -DFMT_HEADER_ONLY '
                   "$variant_flag '{root}/harnesses/autopas_particle_properties.cpp' "
                   'src/autopas/utils/ExceptionHandler.cpp --coverage -pthread -o oracle-ppl/oracle',
  'test_command': 'set -e; out=$(./oracle-ppl/oracle); printf \'%s\\n\' "$out"; test "$out" = \'16 48 16 '
                  "8' ",
  'real_translation_units': ['applicationLibrary/molecularDynamics/molecularDynamicsLibrary/ParticlePropertiesLibrary.h',
                             'src/autopas/utils/ExceptionHandler.cpp'],
  'docker_image': 'reviewbench-hard-native-autopas:20260804',
  'timeout_seconds': 900,
  'build_jobs': 1},
 {'repo': 'LMMS/lmms',
  'input_index': 17,
  'configure_command': 'true',
  'build_command': "set -e; rm -rf oracle-playback; mkdir -p oracle-playback/generated; printf '#pragma "
                   "once\\n#define LMMS_EXPORT\\n' > oracle-playback/generated/lmms_export.h; printf "
                   "'#pragma once\\n#define LMMS_BUILD_LINUX 1\\n' > oracle-playback/generated/lmmsconfig.h; "
                   'extra_source=\'\'; if [ "$ORACLE_VARIANT" = golden ]; then '
                   'extra_source=src/core/AudioResampler.cpp; fi; g++-11 -std=c++17 -O0 -g --coverage -fPIC '
                   '-no-pie -Ioracle-playback/generated -Iinclude $(pkg-config --cflags Qt5Core Qt5Gui '
                   'Qt5Widgets) {root}/harnesses/lmms_playback_state.cpp $extra_source --coverage '
                   '-lsamplerate $(pkg-config --libs Qt5Core Qt5Gui Qt5Widgets) -o oracle-playback/oracle',
  'test_command': 'set -e; out=$(./oracle-playback/oracle); printf \'%s\\n\' "$out"; test "$out" = \'17 1 '
                  "1'",
  'real_translation_units': ['include/Sample.h', 'src/core/AudioResampler.cpp'],
  'docker_image': 'reviewbench-hard-native-lmms:20260804',
  'timeout_seconds': 900,
  'build_jobs': 1},
 {'repo': 'dosbox-staging/dosbox-staging',
  'input_index': 48,
  'configure_command': None,
  'build_command': "set -e; rm -rf build-oracle; mkdir build-oracle; printf '#define HAVE_STRNLEN 1\\n' > "
                   'build-oracle/config.h; variant_flag=\'\'; if test "$ORACLE_VARIANT" = golden; then '
                   'variant_flag=-DDOSBOX_UNIFIED_VMM_API; fi; flags="-std=c++17 -O0 -g --coverage '
                   '-ffunction-sections -fdata-sections $variant_flag -Ibuild-oracle -Iinclude '
                   '-Isrc/hardware/input -Isrc/hardware/serialport"; g++-11 $flags -c '
                   'src/hardware/input/mouseif_virtual_machines.cpp -o build-oracle/vmm.o; g++-11 $flags -c '
                   'src/hardware/input/mouse_common.cpp -o build-oracle/common.o; g++-11 $flags -c '
                   "'{root}/harnesses/dosbox_mouse_vmm.cpp' -o build-oracle/oracle.o; g++-11 --coverage "
                   '-Wl,--gc-sections build-oracle/vmm.o build-oracle/common.o build-oracle/oracle.o -o '
                   'build-oracle/oracle',
  'test_command': 'set -e; out=$(./build-oracle/oracle); printf \'%s\\n\' "$out"; test "$out" = \'32767 '
                  "32767 32767 32767 0 0 0 1'",
  'real_translation_units': ['src/hardware/input/mouseif_virtual_machines.cpp',
                             'src/hardware/input/mouse_common.cpp',
                             'include/mouse.h'],
  'docker_image': 'reviewbench-hard-native:20260804',
  'timeout_seconds': 600,
  'build_jobs': 1},
 {'repo': 'microsoft/vcpkg-tool',
  'input_index': 92,
  'configure_command': 'set -e; cmake -S . -B build-oracle -GNinja -DBUILD_TESTING=OFF '
                       '-DVCPKG_DEVELOPMENT_WARNINGS=OFF -DCMAKE_BUILD_TYPE=Debug '
                       '-DCMAKE_CXX_COMPILER=\'g++-11\'; if [ ! -f "$HOME/fmt-11.0.2/include/fmt/format.h" '
                       ']; then curl -L --retry 3 --fail '
                       'https://github.com/fmtlib/fmt/archive/refs/tags/11.0.2.tar.gz | tar xz -C "$HOME"; '
                       'fi',
  'build_command': 'set -e; rm -rf oracle-message; mkdir oracle-message; if [ "$ORACLE_VARIANT" = golden ]; '
                   'then fmt_inc="$HOME/fmt-11.0.2/include"; else '
                   'fmt_inc="$PWD/build-oracle/_deps/fmt-src/include"; fi; g++-11 -std=c++17 -O0 -g '
                   '--coverage -ffunction-sections -fdata-sections -fpermissive -include unistd.h -include '
                   'vcpkg/base/fmt.h -I"$fmt_inc" -Ibuild-oracle/_cmrc/include -Iinclude '
                   '{root}/harnesses/vcpkg_message_sink.cpp src/vcpkg/base/message_sinks.cpp '
                   'src/vcpkg/base/messages.cpp src/vcpkg/base/stringview.cpp -Wl,--gc-sections --coverage '
                   '-pthread -o oracle-message/oracle',
  'test_command': 'set -e; out=$(./oracle-message/oracle); printf \'%s\\n\' "$out"; test "$out" = '
                  'oracle-vcpkg-line',
  'real_translation_units': ['src/vcpkg/base/message_sinks.cpp',
                             'src/vcpkg/base/messages.cpp',
                             'src/vcpkg/base/stringview.cpp'],
  'docker_image': 'reviewbench-hard-native:20260804',
  'timeout_seconds': 3600,
  'build_jobs': 1},
 {'repo': 'numere-org/NumeRe',
  'input_index': 100,
  'configure_command': None,
  'build_command': 'set -e; git submodule update --init --depth 1 externals/date; rm -rf oracle-number; '
                   'mkdir oracle-number; variant_flag=\'\'; if test "$ORACLE_VARIANT" = golden; then '
                   'variant_flag=-DNUMERE_ORACLE_FORMAT_VOTER; fi; flags="-std=c++17 -O0 -g --coverage '
                   '-ffunction-sections -fdata-sections -D__time64_t=time_t -D_time64=time '
                   '-D_localtime64=localtime $variant_flag -Ikernel/core '
                   '-I{root}/cache/fast_float-v6.1.6/include -Iexternals/date/include"; g++-11 $flags -c '
                   'kernel/core/utils/stringtools.cpp -o oracle-number/stringtools.o; g++-11 $flags -c '
                   "'{root}/harnesses/numere_number_formats.cpp' -o oracle-number/oracle.o; g++-11 "
                   '--coverage -Wl,--gc-sections oracle-number/stringtools.o oracle-number/oracle.o -o '
                   'oracle-number/oracle',
  'test_command': 'set -e; out=$(./oracle-number/oracle); printf \'%s\\n\' "$out"; test "$out" = '
                  "'111111:3,2,1,1,1,0,'",
  'real_translation_units': ['kernel/core/utils/stringtools.cpp'],
  'docker_image': 'reviewbench-hard-native:20260804',
  'timeout_seconds': 900,
  'build_jobs': 2}]
