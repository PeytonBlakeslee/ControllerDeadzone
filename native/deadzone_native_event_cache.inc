// Cache typed event metadata only; argument storage is never retained.
namespace event_decode {
namespace u = unrealsdk::unreal;
struct AxisCache {
    const u::UStruct* owner = nullptr;
    const u::ZNameProperty* key = nullptr;
    const u::ZFloatProperty* delta = nullptr;
    const u::ZBoolProperty* gamepad = nullptr;
    void clear() noexcept { owner=nullptr; key=nullptr; delta=nullptr; gamepad=nullptr; }
    void resolve(const u::UStruct* type) {
        if(type && owner==type) return;
        clear();
        if(!type) throw std::runtime_error("missing InputAxis argument type");
        static const u::FName key_name{L"Key"}, delta_name{L"Delta"}, gamepad_name{L"bGamepad"};
        auto* next_key=type->find_prop_and_validate<u::ZNameProperty>(key_name);
        auto* next_delta=type->find_prop_and_validate<u::ZFloatProperty>(delta_name);
        auto* next_gamepad=type->find_prop_and_validate<u::ZBoolProperty>(gamepad_name);
        if(!next_key || !next_delta || !next_gamepad)
            throw std::runtime_error("incomplete InputAxis argument metadata");
        key=next_key; delta=next_delta; gamepad=next_gamepad; owner=type;
    }
};
struct KeyCache {
    const u::UStruct* owner = nullptr;
    const u::ZNameProperty* key = nullptr;
    const u::ZByteProperty* event = nullptr;
    const u::ZBoolProperty* gamepad = nullptr;
    void clear() noexcept { owner=nullptr; key=nullptr; event=nullptr; gamepad=nullptr; }
    void resolve(const u::UStruct* type) {
        if(type && owner==type) return;
        clear();
        if(!type) throw std::runtime_error("missing InputKey argument type");
        static const u::FName key_name{L"Key"}, event_name{L"EventType"}, gamepad_name{L"bGamepad"};
        auto* next_key=type->find_prop_and_validate<u::ZNameProperty>(key_name);
        auto* next_event=type->find_prop_and_validate<u::ZByteProperty>(event_name);
        auto* next_gamepad=type->find_prop_and_validate<u::ZBoolProperty>(gamepad_name);
        if(!next_key || !next_event || !next_gamepad)
            throw std::runtime_error("incomplete InputKey argument metadata");
        key=next_key; event=next_event; gamepad=next_gamepad; owner=type;
    }
};
AxisCache axis{};
KeyCache key{};
void clear() noexcept { axis.clear(); key.clear(); }
} // namespace event_decode
