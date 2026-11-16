// Stage 20: native ownership ledger and verified controller field I/O on verified InputDevice_Controller.
// No cached UObject pointers. Python validates live owner, device/index, and
// physical mapping before each call. Native repeats all identity/type checks.
namespace axis_fields {
namespace u=unrealsdk::unreal;
using OwnerKey = std::tuple<uintptr_t,int,int,int>;
struct OwnedValue {
 double baseline=0.0,applied=0.0,pending=0.0;
 bool uncertain=false;
};
std::map<OwnerKey,OwnedValue> owned{}; // metadata only; NEVER cache UObject pointers
OwnerKey owner_key(uintptr_t addr,int index,int physical,int which) {
 return {addr,index,physical,which};
}
void clear_owners() noexcept {owned.clear();}
void relinquish(uintptr_t device,int index,int physical,int which) noexcept {
 owned.erase(owner_key(device,index,physical,which));
}

constexpr double EPS=1e-6;
constexpr const wchar_t* PHYSICAL[4]={
 L"XboxTypeS_LeftX",L"XboxTypeS_LeftY",L"XboxTypeS_RightX",L"XboxTypeS_RightY"};
struct Checked {
  u::WrappedArray axes;
  u::WrappedStruct row;
  static u::UObject* verify(u::UObject* obj,int index,int physical) {
      if(!obj || physical<0 || physical>3 || index<0 || sizeof(void*)!=4)
         throw std::runtime_error("invalid verified controller source");
      if(obj->Class()->Name()!=u::FName{L"InputDeviceDefinition"} ||
         obj->Name()!=u::FName{L"InputDevice_Controller"})
         throw std::runtime_error("controller source identity changed");
      return obj;
  }
  Checked(u::UObject* obj,int index,int physical):
    axes(verify(obj,index,physical)->get<u::ZArrayProperty>(u::FName{L"Axes"})),
    row(axes.get_at<u::ZStructProperty>(static_cast<size_t>(index))) {
      if(row.get<u::ZNameProperty>(u::FName{L"AxisName"})!=u::FName{PHYSICAL[physical]})
         throw std::runtime_error("controller physical axis mapping changed");
    }
};
u::FName field(int which) {
  if(which==0)return u::FName{L"Speed"};
  if(which==1)return u::FName{L"DeadZone"};
  throw std::runtime_error("unknown controller field");
}
bool finite_value(double v,int which) {
  return std::isfinite(v) && ((which==0 && v>-10000.0 && v<10000.0) ||
        (which==1 && v>=0.0 && v<=1.0));
}
// Return (status,value). 1=success, 0=not readable: caller uses old safe path.
py::tuple read(uintptr_t device,int index,int physical,int which) {

  try {
    if(!device || index<0 || physical<0 || physical>3)
      throw std::runtime_error("invalid source");
    auto* obj=reinterpret_cast<u::UObject*>(device);
    Checked row(obj,index,physical);
    const double val=row.row.get<u::ZFloatProperty>(field(which));
    if(!finite_value(val,which))throw std::runtime_error("invalid scalar");
    return py::make_tuple(1,val);
  } catch(...) {
    return py::make_tuple(0,0.0);
  }
}
// Result: 1=written/verified; 2=already desired; 0=CAS conflict/no mutation;
// -1=read/write failure (could be partially written; do NOT retry blindly).
int cas(uintptr_t device,int index,int physical,int which,
        double expected,double desired) {

  try {
    if(!device || !finite_value(expected,which) || !finite_value(desired,which))
      throw std::runtime_error("invalid CAS source/value");
    auto* obj=reinterpret_cast<u::UObject*>(device);
    Checked entry(obj,index,physical);
    const auto fname=field(which);
    const double current=entry.row.get<u::ZFloatProperty>(fname);
    if(!finite_value(current,which))throw std::runtime_error("invalid current field");
    if(std::abs(current-expected)>EPS){return 0;}
    auto id=owner_key(device,index,physical,which);
    auto prior=owned.find(id);
    if(prior!=owned.end()&&prior->second.uncertain)
      throw std::runtime_error("unverified owned field requires restoration");
    if(prior!=owned.end() && std::abs(current-prior->second.applied)>EPS) {
      return 0;
    }
    if(std::abs(current-desired)<=EPS){return 2;}
    // Save intent BEFORE touching the engine: failed readback still needs an
    // owner record for verified restoration, never a second filtering pass.
    if(prior==owned.end()) {
      prior=owned.emplace(id,OwnedValue{expected,current,desired,true}).first;

    } else {
      prior->second.pending=desired;prior->second.uncertain=true;
    }
    entry.row.set<u::ZFloatProperty>(fname,static_cast<float>(desired));
    // Re-fetch through UObject.Axes: a wrapped struct could be a copy.
    Checked fresh(obj,index,physical);
    const double observed=fresh.row.get<u::ZFloatProperty>(fname);
    if(!finite_value(observed,which)||std::abs(observed-desired)>EPS){
      throw std::runtime_error("native field readback mismatch");
    }

    prior->second.applied=desired;prior->second.uncertain=false;
    if(std::abs(desired-prior->second.baseline)<=EPS) {
      owned.erase(prior);
    }
    return 1;
  }catch(...){
    return -1;
  }
}
// Return: 1=restored; 2=not tracked; 0=external conflict; -1=unverified.
int restore_owned(uintptr_t device,int index,int physical,int which,double original) {
  auto it=owned.find(owner_key(device,index,physical,which));
  if(it==owned.end()){return 2;}
  if(!finite_value(original,which) || std::abs(original-it->second.baseline)>EPS) {
    return 0;
  }
  try {
    auto* obj=reinterpret_cast<u::UObject*>(device);
    Checked entry(obj,index,physical);
    auto fname=field(which);
    const double current=entry.row.get<u::ZFloatProperty>(fname);
    if(!finite_value(current,which))throw std::runtime_error("invalid restore field");
    if(std::abs(current-original)<=EPS) {
      owned.erase(it);return 1;
    }
    if(std::abs(current-it->second.applied)>EPS &&
       !(it->second.uncertain&&std::abs(current-it->second.pending)<=EPS)){

      owned.erase(it); // relinquish ownership, never undo external changes
      return 0;
    }
    entry.row.set<u::ZFloatProperty>(fname,static_cast<float>(original));
    Checked again(obj,index,physical);
    const double observed=again.row.get<u::ZFloatProperty>(fname);
    if(!finite_value(observed,which)||std::abs(observed-original)>EPS) {
      throw std::runtime_error("native owner restore readback mismatch");
    }
    owned.erase(it);return 1;
  } catch(...) {
    return -1;
  }
}
} // namespace axis_fields
