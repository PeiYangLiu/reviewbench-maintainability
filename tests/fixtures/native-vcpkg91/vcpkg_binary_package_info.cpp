#include <vcpkg/base/checks.h>
#include <vcpkg/binarycaching.h>
#include <vcpkg/build.h>
#include <vcpkg/dependencies.h>
#include <vcpkg/sourceparagraph.h>

#include <iostream>
#include <memory>

namespace vcpkg::Checks
{
    void on_final_cleanup_and_exit() { }
}

int main()
{
    vcpkg::SourceControlFileAndLocation source;
    source.source_control_file = std::make_unique<vcpkg::SourceControlFile>();
    source.source_control_file->core_paragraph = std::make_unique<vcpkg::SourceParagraph>();
    source.source_control_file->core_paragraph->raw_version = "1.2.3";

    std::string triplet_abi = "triplet-abi";
    vcpkg::CompilerInfo compiler{"gcc", "11.4", "compiler-hash"};

    vcpkg::InstallPlanAction action;
    action.source_control_file_and_location.emplace(source);
    auto& abi = action.abi_info.emplace();
    abi.package_abi = "package-abi";
    abi.triplet_abi.emplace(triplet_abi);
    abi.compiler_info.emplace(compiler);

    const vcpkg::BinaryPackageInformation information(action);
    std::cout << information.package_abi << ' ' << information.raw_version << '\n';
    return 0;
}
