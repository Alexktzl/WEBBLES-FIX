struct Holder { data: String }
fn take(h: &Holder) -> String {
    h.data.clone()
}
fn main() {}
