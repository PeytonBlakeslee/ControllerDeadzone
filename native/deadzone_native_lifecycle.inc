// Stage 22: local PlayerInput lifecycle invalidation. No UObject pointers
// retained, no engine binding fields modified, and PRE never blocks setup.
namespace lifecycle {
namespace hm=unrealsdk::hook_manager;
constexpr const wchar_t* PATHS[]={
 L"WillowGame.WillowPlayerInput:SetupInputDevices",
 L"WillowGame.WillowPlayerInput:InitInputSystem",
 L"WillowGame.WillowPlayerInput:PostChangeInputContext"
};
constexpr const wchar_t* ID_LABELS[]={
 L"ControllerDeadzoneControl:NativeLifecycle:Setup",
 L"ControllerDeadzoneControl:NativeLifecycle:Init",
 L"ControllerDeadzoneControl:NativeLifecycle:Context"
};
std::array<std::wstring,3> IDs{};
std::array<bool,3> registered{false,false,false};
std::array<bool,3> pending_removal{false,false,false};
uintptr_t verified_player_input=0;
bool suspended=false;
// Disarm native frame processing before binding mutations. Do not discard
// the ownership ledger until Python POST has restored/relinquished old fields.
bool suspend_for(uintptr_t owner,bool) noexcept {
 if(!verified_player_input||!owner||owner!=verified_player_input){
   return false;
 }
 if(!suspended){
   suspended=true;
   preprocess_hook::suspend();
   backend.reset(); // cached reflection/input samples, NOT owned fields
 }
 return true;
}
bool callback(hm::Details& args) noexcept {
 if(args.obj)suspend_for(reinterpret_cast<uintptr_t>(args.obj),true);
 return false;
}
bool start(uintptr_t verified_pi){
 if(!verified_pi||sizeof(void*)!=4)return false;
 if(verified_player_input&&verified_player_input!=verified_pi){
   suspended=true;preprocess_hook::suspend();
 }
 verified_player_input=verified_pi;
 bool any=false;
 for(size_t i=0;i<3;++i){
   if(pending_removal[i])return false;
   if(registered[i]&&!hm::has_hook(PATHS[i],hm::Type::PRE,IDs[i].c_str()))registered[i]=false;
   if(registered[i]){any=true;continue;}
   IDs[i]=next_hook_id(ID_LABELS[i]);
   if(hm::has_hook(PATHS[i],hm::Type::PRE,IDs[i].c_str())){
     continue;
   }
   registered[i]=true; // preserve possible insertion if add throws
   hm::add_hook(PATHS[i],hm::Type::PRE,IDs[i].c_str(),&callback);
   registered[i]=hm::has_hook(PATHS[i],hm::Type::PRE,IDs[i].c_str());
   if(registered[i])any=true;
 }
 return any&&std::all_of(registered.begin(),registered.end(),[](bool yes){return yes;});
}
bool begin(uintptr_t verified_pi) noexcept {
 return suspend_for(verified_pi,false);
}
bool resume(uintptr_t verified_pi) noexcept {
 if(!verified_pi||verified_pi!=verified_player_input){
   return false;
 }
 if(!preprocess_hook::prepared())return false;
 // Check the actual rebuilt UObject bindings before promoting the native
 // frame hook. The scan does not retain engine pointers or modify properties.
 try {
   auto result=binding_discovery::scan(verified_pi);
   const bool ready=result[0].cast<int>()==1 &&
                    py::len(result[1])==4 && py::len(result[2])==0;
   if(!ready){
     preprocess_hook::suspend();
     return false;
   }
 }catch(...){
   preprocess_hook::suspend();
   return false;
 }
 suspended=false;return preprocess_hook::activate(verified_pi);
}
bool active() noexcept {
 return verified_player_input&&!suspended&&
   std::any_of(registered.begin(),registered.end(),[](bool yes){return yes;});
}
void stop() noexcept {
 verified_player_input=0;suspended=true;
 for(size_t i=0;i<3;++i){
   if(!registered[i])continue;
   pending_removal[i]=true;
   try{
     const bool removed=hm::remove_hook(PATHS[i],hm::Type::PRE,IDs[i].c_str());
     // True confirms removal; never retain a later foreign replacement.
     registered[i]=!removed&&hm::has_hook(PATHS[i],hm::Type::PRE,IDs[i].c_str());
     if(!registered[i])pending_removal[i]=false;
   }
   catch(...){ /* owner=0 ensures callbacks are inert */ }
 }
}
} // namespace lifecycle
