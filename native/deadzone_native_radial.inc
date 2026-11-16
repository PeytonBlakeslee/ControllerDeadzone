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
    const u::ZFloatProperty* find(const u::UStruct* cls, const u::FName& name) {
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
    deadzone::InputState input{};
    bool quarantined=false;

    void reset() noexcept {
        for (auto& entry: cache) entry.clear();
        event_decode::clear();
        quarantined=false;
        input.reset();
    }

    // 1 = handled. 0 = no engine changes; Python may run this frame.
    // -1 = possible partial native changes; DO NOT run Python this frame.
    int apply(std::uintptr_t address, bool enable_left, bool enable_right,
              double lx, double ly, double rx, double ry,
              double li, double lo, double ri, double ro) {
        if (!address || sizeof(void*) != 4 || quarantined) {
            return 0;
        }
        Filter l,r;
        {
            l=enable_left ? multiplier(lx,ly,li,lo) : Filter{};
            r=enable_right ? multiplier(rx,ry,ri,ro) : Filter{};
        }
        if ((enable_left && !l.valid) || (enable_right && !r.valid)) {
            return 0;
        }
        bool began_write=false;
        try {
            // The Python caller has validated current local ownership.
            // This raw address is used only in this synchronous game-thread call.
            auto* pi=reinterpret_cast<u::UObject*>(address);
            std::array<const u::ZFloatProperty*,FIELDS> props{};
            std::array<float,FIELDS> before{};
            std::array<float,FIELDS> after{};
            {
            const auto* cls=pi->Class();
            static const std::array<u::FName,FIELDS> names = {
                u::FName{L"aStrafe"},u::FName{L"aForward"},
                u::FName{L"aBaseY"},u::FName{L"RawJoyRight"},
                u::FName{L"RawJoyUp"},u::FName{L"aTurn"},
                u::FName{L"aLookUp"},u::FName{L"RawJoyLookRight"},
                u::FName{L"RawJoyLookUp"}
            };
            // Complete all reflection reads before writing any value.
            for (int i=0;i<FIELDS;++i) {
                if ((i<5 && !enable_left) || (i>=5 && !enable_right)) continue;
                props[i]=cache[i].find(cls,names[i]);
                before[i]=pi->get<u::ZFloatProperty>(props[i]);
                if (!std::isfinite(before[i])) throw std::runtime_error("nonfinite input field");
                const double next=static_cast<double>(before[i]) *
                                  (i<5 ? l.factor : r.factor);
                if (!std::isfinite(next)) throw std::runtime_error("nonfinite radial result");
                after[i]=static_cast<float>(next);
            }
            }
            {
            for (int i=0;i<FIELDS;++i) {
                if (!props[i] || before[i]==after[i]) continue;
                began_write=true;
                pi->set<u::ZFloatProperty>(props[i],after[i]);
            }
            }
            return 1;
        } catch (...) {
            // Return a failure status so Python can fall back safely.
        }
        if (began_write) {
            quarantined=true;
            return -1;
        }
        return 0;
    }


};
