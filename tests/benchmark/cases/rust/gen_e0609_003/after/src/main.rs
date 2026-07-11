pub struct Order { pub status: u32 }
fn main() {
    let v = Order { status: 1 };
    println!("{}", v.status);
}
