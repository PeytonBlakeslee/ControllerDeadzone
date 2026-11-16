// Stage 18: direct native PreProcessInput radial hook.
// Python authenticates the local PlayerInput and verified physical-source
// mappings before this hook is armed. No Python code runs for ordinary frames.
namespace preprocess_hook {
namespace hm=unrealsdk::hook_manager;
namespace u=unrealsdk::unreal;
constexpr wchar_t FUNCTION[]=L"WillowGame.WillowPlayerInput:PreProcessInput";
std::wstring IDENTIFIER;
struct Config {
    uintptr_t player_input=0;
    bool left=false,right=false,needs_native_key=false;
    double left_inner=0,left_outer=1,right_inner=0,right_outer=1;
};
Config config{};
bool installed=false,enabled=false;
bool pending_removal=false;
py::object fault_receiver{};
const u::UStruct* using_gamepad_owner=nullptr;
const u::ZBoolProperty* using_gamepad_property=nullptr;

void signal_failure(const std::string& message) noexcept {
    enabled=false;
    try {
        if(!fault_receiver.is_none()) {
            py::gil_scoped_acquire gil{};
            fault_receiver(message);
        }
    } catch(...) { /* already disarmed: fail closed, never crash input */ }
}

bool callback(hm::Details& h) noexcept {
    if(!enabled || !h.obj)return false;
    if(config.player_input==0 || reinterpret_cast<uintptr_t>(h.obj)!=config.player_input) {
        return false;
    }
    try {
        double now;
        {
        if(!axis_hook::active()) {
            signal_failure("native InputAxis hook is no longer active");
            return false;
        }
        if(config.needs_native_key && !key_hook::active()) {
            signal_failure("native InputKey hook is no longer active");
            return false;
        }
        auto* klass=h.obj->Class();
        if(klass!=using_gamepad_owner || using_gamepad_property==nullptr) {
            using_gamepad_property=klass->find_prop_and_validate<u::ZBoolProperty>(
                u::FName{L"bUsingGamepad"});
            if(!using_gamepad_property)throw std::runtime_error("missing bUsingGamepad");
            using_gamepad_owner=klass;
        }
        const bool gamepad=h.obj->get<u::ZBoolProperty>(using_gamepad_property);
        now=monotonic_seconds();
        if(!backend.input.frame_allowed(gamepad,now)) {
            return false;
        }
        }
        const double lx=config.left?backend.input.sample(0,now):0.0;
        const double ly=config.left?backend.input.sample(1,now):0.0;
        const double rx=config.right?backend.input.sample(2,now):0.0;
        const double ry=config.right?backend.input.sample(3,now):0.0;
        const int result=backend.apply(config.player_input,config.left,config.right,
                                       lx,ly,rx,ry,config.left_inner,
                                       config.left_outer,config.right_inner,
                                       config.right_outer);
        if(result!=1) {
            signal_failure(result==-1 ? "partial native radial write; quarantined" :
                           "native radial backend unavailable; reverting to Python");
        }
    } catch(const std::exception& e) {signal_failure(e.what());}
      catch(...) {signal_failure("unknown native PreProcessInput exception");}
    return false; // PRE hook must never block BL2's input processing.
}

bool start(uintptr_t player_input,bool left,bool right,double li,double lo,
           double ri,double ro,bool native_key,py::object fallback) {
    if(player_input==0 || (!left && !right) || !PyCallable_Check(fallback.ptr()))return false;
    if(pending_removal)return false;
    if(installed && !hm::has_hook(FUNCTION,hm::Type::PRE,IDENTIFIER.c_str()))installed=false;
    if(!installed) {
        IDENTIFIER=next_hook_id(L"ControllerDeadzoneControl:NativeRadial158");
        if(hm::has_hook(FUNCTION,hm::Type::PRE,IDENTIFIER.c_str())) {
            return false;
        }
        installed=true;  // add may throw after insertion
        hm::add_hook(FUNCTION,hm::Type::PRE,IDENTIFIER.c_str(),&callback);
        if(!hm::has_hook(FUNCTION,hm::Type::PRE,IDENTIFIER.c_str())) {
            installed=false;
            return false;
        }
        installed=true;
    }
    config={player_input,left,right,native_key,li,lo,ri,ro};
    fault_receiver=std::move(fallback);
    using_gamepad_owner=nullptr;
    using_gamepad_property=nullptr;
    // Registration prepares a context only. Lifecycle validation must arm it.
    enabled=false;
    return true;
}
bool prepared() noexcept {return installed&&config.player_input!=0;}
bool activate(uintptr_t verified_pi) noexcept {
    if(!prepared()||config.player_input!=verified_pi)return false;
    enabled=true;return true;
}
void suspend() noexcept { enabled=false;config.player_input=0; }
bool stop() noexcept {
    suspend();
    if(Py_IsInitialized()&&PyGILState_Check())fault_receiver=py::none();
    using_gamepad_property=nullptr;using_gamepad_owner=nullptr;
    if(!installed)return false;
    pending_removal=true;
    try {
        const bool removed=hm::remove_hook(FUNCTION,hm::Type::PRE,IDENTIFIER.c_str());
        // True confirms our removal. Do not query and retain a replacement.
        installed=!removed&&hm::has_hook(FUNCTION,hm::Type::PRE,IDENTIFIER.c_str());
        if(!installed)pending_removal=false;
        return removed;
    }
    catch(...) {return false;}
}
bool active() noexcept {return installed&&enabled&&config.player_input!=0;}
} // namespace preprocess_hook
