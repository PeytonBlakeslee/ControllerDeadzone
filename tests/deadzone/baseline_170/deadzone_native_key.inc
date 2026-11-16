// Native Win32 InputKey PRE callback for verified radial-only C++ sessions.
// Engine-side event parsing and held-key updates happen without Python.
// Python is notified only when device owner/guard state changes.
namespace key_hook {
namespace hm=unrealsdk::hook_manager;
namespace u=unrealsdk::unreal;
constexpr wchar_t FUNCTION[]=L"WillowGame.WillowGameViewportClient:InputKey";
constexpr wchar_t IDENTIFIER[]=L"ControllerDeadzoneControl:NativeKey157";
bool installed=false, enabled=false;
py::object notification{};
std::uint64_t callbacks=0,decoded=0,notifications=0,unchanged=0,failures=0;
std::uint64_t press=0,release=0,repeat=0;
int registration_result=0;
std::string error{};
bool have_guard=false, left_guard=false, right_guard=false;

bool callback(hm::Details& h) noexcept {
    ++callbacks;
    if(!enabled || !h.args)return false;
    try {
        static const u::FName key_prop{L"Key"}, event_prop{L"EventType"},
            gamepad_prop{L"bGamepad"};
        const u::FName name=h.args->get<u::ZNameProperty>(key_prop);
        const auto event=h.args->get<u::ZByteProperty>(event_prop);
        const bool gamepad=h.args->get<u::ZBoolProperty>(gamepad_prop);
        const int kind=event==0?0:event==1?1:event==2?2:event==3?3:4;
        if(kind==0||kind==3)++press;
        else if(kind==1)++release;
        else if(kind==2)++repeat;
        const int previous=static_cast<int>(backend.input.owner);
        const int current=backend.input.key(std::string{name},gamepad,kind,
                                            monotonic_seconds());
        ++decoded;
        const auto guard=backend.input.guard(monotonic_seconds());
        const bool changed=current!=previous;
        const bool guard_changed=!have_guard||left_guard!=guard.first||
                                   right_guard!=guard.second;
        have_guard=true;left_guard=guard.first;right_guard=guard.second;
        if((changed||guard_changed)&&!notification.is_none()) {
            py::gil_scoped_acquire gil{};
            notification(current,guard.first,guard.second,changed);
            ++notifications;
        } else ++unchanged;
    } catch(const std::exception& e) {
        ++failures;error=e.what();enabled=false;
    } catch(...) {
        ++failures;error="unrecognized native InputKey exception";enabled=false;
    }
    return false; // Never intercept or block a game input.
}

bool start(py::object receiver) {
    if(!PyCallable_Check(receiver.ptr()))return false;
    if(installed){notification=std::move(receiver);enabled=true;return true;}
    if(hm::has_hook(FUNCTION,hm::Type::PRE,IDENTIFIER)){
        registration_result=-2;return false;
    }
    const bool reported=hm::add_hook(FUNCTION,hm::Type::PRE,IDENTIFIER,&callback);
    if(!hm::has_hook(FUNCTION,hm::Type::PRE,IDENTIFIER)){
        registration_result=-3;return false;
    }
    notification=std::move(receiver);
    have_guard=false;
    installed=true;enabled=true;registration_result=reported?1:2;
    return true;
}
bool stop() noexcept {
    enabled=false;
    if(Py_IsInitialized()&&PyGILState_Check())notification=py::none();
    have_guard=false;
    if(!installed)return false;
    try{
        const bool removed=hm::remove_hook(FUNCTION,hm::Type::PRE,IDENTIFIER);
        installed=hm::has_hook(FUNCTION,hm::Type::PRE,IDENTIFIER);
        return removed;
    }
    catch(...){return false;}
}
bool active() noexcept {return installed&&enabled;}
void stats(py::dict& out) {
    out["native_key_hook_active"]=active();
    out["native_key_hook_calls"]=callbacks;
    out["native_key_hook_decoded"]=decoded;
    out["native_key_hook_python_notifications"]=notifications;
    out["native_key_hook_unchanged_events"]=unchanged;
    out["native_key_hook_failures"]=failures;
    out["native_key_hook_presses"]=press;
    out["native_key_hook_releases"]=release;
    out["native_key_hook_repeats"]=repeat;
    out["native_key_hook_registration"]=registration_result;
    out["native_key_hook_error"]=error;
}
} // namespace key_hook
