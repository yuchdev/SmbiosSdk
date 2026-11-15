// Clean-room consumer smoke test for the installed libsmbios package.
//
// Closes the ADR 0004 DD1 footgun: entry classes and output formatters
// register themselves from static initializers, which only run for object
// files that are actually linked in. This file names NO concrete entry class
// and NO formatter class; it only asks the registries. If the exported target
// lost its whole-archive link option, the factory returns nullptr and the
// formatter list is empty, and this program exits non-zero.
#include <libsmbios/abstract_smbios_entry.hpp>
#include <libsmbios/output_formatter.hpp>
#include <libsmbios/smbios.hpp>
#include <libsmbios/smbios_entry_factory.hpp>

#include <cstdint>
#include <iostream>
#include <vector>

int main()
{
    // 1) Output formatters (text/xml/json/yaml) are registered.
    const auto names = smbios::registered_output_formatter_names();
    std::cout << "formatters=" << names.size() << '\n';
    if (names.empty()) {
        std::cerr << "no output formatter registered: whole-archive link lost\n";
        return 1;
    }

    // 2) A structure type never named here (17, Memory Device) is created by
    //    the factory. Synthetic bytes: SMBIOS 2.3 formatted area (0x15 bytes,
    //    all zero apart from type/length/handle), then the empty string
    //    section (two NULs). No hardware, no privileges.
    std::vector<uint8_t> table(0x15, 0);
    table[0] = 17;
    table[1] = 0x15;
    table[2] = 0x00;
    table[3] = 0x11;
    table.push_back(0);
    table.push_back(0);

    smbios::DMIHeader header{};
    header.type = table[0];
    header.length = table[1];
    header.handle = static_cast<uint16_t>(table[2] | (table[3] << 8));
    header.data = table.data();

    const smbios::SMBiosVersion version{2, 3};
    smbios::SMBiosEntryFactory factory;
    const auto entry = factory.create(header, version, table.data() + table.size());
    if (!entry) {
        std::cerr << "factory returned nullptr for type 17: whole-archive link lost\n";
        return 1;
    }
    std::cout << "type17=" << entry->get_type() << '\n';
    if (entry->get_fields().empty()) {
        std::cerr << "type 17 entry rendered no fields\n";
        return 1;
    }
    std::cout << "fields=" << entry->get_fields().size() << '\n';
    return 0;
}
