struct Filter {
    double factor = 0.0;
    bool valid = false;
};
Filter multiplier(double x, double y, double inner, double outer) noexcept {
    if (!std::isfinite(x) || !std::isfinite(y) || !std::isfinite(inner) ||
        !std::isfinite(outer) || inner < 0.0 || inner > 1.0 ||
        outer < 0.0 || outer > 1.0 || std::abs(x) > 1.0 ||
        std::abs(y) > 1.0) return {};
    if (inner >= 1.0) return {0.0, true};
    const double radius = std::hypot(x, y);
    if (radius <= inner || radius <= 0.0) return {0.0, true};
    const double mapped = outer <= inner ? 1.0 :
        std::min(1.0, (radius - inner) / (outer - inner));
    return {mapped / radius, true};
}

struct CachedFloat {
    const u::UStruct* owner = nullptr;
    const u::ZFloatProperty* property = nullptr;
    const u::ZFloatProperty* find(u::UObject* object, const u::FName& name) {
        const auto* cls = object->Class();
        if (owner != cls || !property) {
            const auto* checked = cls->find_prop_and_validate<u::ZFloatProperty>(name);
            if (checked == nullptr) throw std::runtime_error("missing reflected float property");
            owner = cls;
            property = checked;
        }
        return property;
    }
    void clear() noexcept { owner = nullptr; property = nullptr; }
};

struct Backend {
    std::array<CachedFloat,FIELDS> cache{};
    std::uint64_t calls=0, valid=0, unavailable=0;
    std::uint64_t left=0, right=0, writes=0;
    std::uint64_t full_block=0, center_block=0;
    std::uint64_t reflection_errors=0, partial_errors=0;
    deadzone::InputState input{};
    bool quarantined=false;
    std::string last_error;

    void reset() noexcept {
        for (auto& entry: cache) entry.clear();
        quarantined=false;
        last_error.clear();
        input.reset();
    }

    // 1 = handled. 0 = no engine changes; Python may run this frame.
    // -1 = possible partial native changes; DO NOT run Python this frame.
    int apply(std::uintptr_t address, bool enable_left, bool enable_right,
              double lx, double ly, double rx, double ry,
              double li, double lo, double ri, double ro) {
        ++calls;
        if (!address || sizeof(void*) != 4 || quarantined) {
            ++unavailable;
            return 0;
        }
        const Filter l=multiplier(lx,ly,li,lo);
        const Filter r=multiplier(rx,ry,ri,ro);
        if ((enable_left && !l.valid) || (enable_right && !r.valid)) {
            ++unavailable;
            last_error="invalid paired controller sample or radial settings";
            return 0;
        }
        bool began_write=false;
        try {
            // The Python caller has validated current local ownership.
            // This raw address is used only in this synchronous game-thread call.
            auto* pi=reinterpret_cast<u::UObject*>(address);
            static const std::array<u::FName,FIELDS> names = {
                u::FName{L"aStrafe"},u::FName{L"aForward"},
                u::FName{L"aBaseY"},u::FName{L"RawJoyRight"},
                u::FName{L"RawJoyUp"},u::FName{L"aTurn"},
                u::FName{L"aLookUp"},u::FName{L"RawJoyLookRight"},
                u::FName{L"RawJoyLookUp"}
            };
            std::array<const u::ZFloatProperty*,FIELDS> props{};
            std::array<float,FIELDS> before{};
            std::array<float,FIELDS> after{};
            // Complete all reflection reads before writing any value.
            for (int i=0;i<FIELDS;++i) {
                if ((i<5 && !enable_left) || (i>=5 && !enable_right)) continue;
                props[i]=cache[i].find(pi,names[i]);
                before[i]=pi->get<u::ZFloatProperty>(props[i]);
                if (!std::isfinite(before[i])) throw std::runtime_error("nonfinite input field");
                const double next=static_cast<double>(before[i]) *
                                  (i<5 ? l.factor : r.factor);
                if (!std::isfinite(next)) throw std::runtime_error("nonfinite radial result");
                after[i]=static_cast<float>(next);
            }
            for (int i=0;i<FIELDS;++i) {
                if (!props[i] || before[i]==after[i]) continue;
                began_write=true;
                pi->set<u::ZFloatProperty>(props[i],after[i]);
                ++writes;
            }
            ++valid;
            if (enable_left) {
                ++left;
                if (li>=1.0) ++full_block;
                else if (l.factor==0.0) ++center_block;
            }
            if (enable_right) {
                ++right;
                if (ri>=1.0) ++full_block;
                else if (r.factor==0.0) ++center_block;
            }
            return 1;
        } catch (const std::exception& e) {
            ++reflection_errors;
            last_error=e.what();
        } catch (...) {
            ++reflection_errors;
            last_error="unrecognized C++ exception";
        }
        if (began_write) {
            ++partial_errors;
            quarantined=true;
            return -1;
        }
        ++unavailable;
        return 0;
    }

    // Diagnostic-only snapshot omitted: player InputState has no counters.

};
